from __future__ import annotations

import logging
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple, TypedDict, Literal

from django import forms
from django.http.request import HttpRequest
from django.utils.translation import gettext_lazy as _

from sentry.integrations.base import (
    FeatureDescription,
    IntegrationData,
    IntegrationFeatures,
    IntegrationMetadata,
    IntegrationProvider,
)
from sentry.integrations.models.integration import Integration
from sentry.integrations.messaging.integration import MessagingIntegration
from sentry.shared_integrations.exceptions import IntegrationError
from sentry.http import safe_urlopen
from sentry.models.group import Group
from sentry.event_manager import Event
from sentry.types.integrations import ExternalProviders
from sentry.types.alert import Alert, AlertCategory
from sentry.notifications.notification_options import NotificationSetting, NotificationOption

logger = logging.getLogger("sentry.integrations.telegram_routing")

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
EVENT_TITLE_MAX_LENGTH = 500

class TelegramChannelConfig(TypedDict):
    api_token: str
    receivers: str
    template: str
    filters: list[dict[str, Any]]

class TelegramChannelsConfigJson(TypedDict):
    channels: list[TelegramChannelConfig]
    api_origin: str

class TelegramRoutingIntegrationConfigForm(forms.Form):
    """
    Форма для настройки интеграции в UI Sentry.
    """
    api_origin = forms.CharField(
        label=_("Telegram API origin"),
        widget=forms.TextInput(attrs={"placeholder": "https://api.telegram.org"}),
        initial="https://api.telegram.org",
        help_text=_("The base URL for the Telegram Bot API. Defaults to https://api.telegram.org.")
    )
    channels_config_json = forms.CharField(
        label=_("Channels Configuration (JSON)"),
        widget=forms.Textarea(attrs={"class": "span10", "rows": 15}),
        help_text=_(
            "JSON configuration for routing messages to different channels. "
            "Each channel can have its own API token, receivers, message template, and filters. "
            "If no filters are specified for a channel, it acts as a default fallback. "
        ),
        required=True
    )
    default_message_template = forms.CharField(
        label=_("Default Message Template"),
        widget=forms.Textarea(attrs={"class": "span4"}),
        help_text=_(
            "Set in standard Python's {}-format convention. "
            "Available names are: {project_name}, {url}, {title}, {message}, {tag[%your_tag%]}. "
            "Undefined tags will be shown as [NA]. This template is used if a specific channel template is empty."
        ),
        initial="*[Sentry]* {project_name} {tag[level]}: *{title}*\n```\n{message}```\n{url}",
        required=True
    )

    def clean(self):
        cleaned_data = super().clean()
        channels_config_json = cleaned_data.get("channels_config_json")
        if channels_config_json:
            try:
                config: TelegramChannelsConfigJson = json.loads(channels_config_json)
                if "channels" not in config or not isinstance(config["channels"], list):
                    raise forms.ValidationError(
                        _("Channels configuration must contain a 'channels' key with a list of channel objects.")
                    )
            except json.JSONDecodeError as e:
                raise forms.ValidationError(
                    _("Invalid JSON in Channels Configuration: %s. Please check your syntax.") % e
                )
        return cleaned_data

class ExampleSetupView(IntegrationPipelineViewT):
    TEMPLATE = """
        <form method="POST">
            <p>This is the setup page for Telegram Routing.</p>
            <p><label>You will configure the routing rules in the next step.</label></p>
            <p><input type="submit" value="Continue" /></p>
        </form>
    """

    def dispatch(self, request: HttpRequest, pipeline: IntegrationPipelineT) -> HttpResponse:
        if request.method == "POST":
            return pipeline.next_step()
        return HttpResponse(self.TEMPLATE)


class TelegramRoutingIntegration(MessagingIntegration):
    provider = "telegram_routing_plus"

    def get_client(self, access_token: str | None = None) -> Any:
        raise NotImplementedError("Telegram API client is channel-specific.")

    def get_form_config(self, organization):

        return TelegramRoutingIntegrationConfigForm

    def get_message_context(self, notification: Alert, event: Event | None) -> dict[str, Any]:

        if event:
            project_name = event.project.slug if event.project else "unknown-project"
            title = event.title
            message = event.message or ""
            url = event.get_absolute_url(
                params={"referrer": f"telegram_routing_plus-integration"}
            )
            tags = {tag.key: tag.value for tag in event.tags}
        else:
            project_name = notification.project.slug if notification.project else "unknown-project"
            title = notification.get_subject()
            message = notification.message or ""
            url = notification.url
            tags = {}

        class TagDict(dict):
            def __getitem__(self, key):
                return self.get(key, "[NA]")

        context = {
            "project_name": project_name,
            "url": url,
            "title": title,
            "message": message,
            "tag": TagDict(tags),
            "event": event,
            "notification": notification
        }
        return context


    def _render_message(self, template: str, context: Mapping[str, Any]) -> str:
        try:
            def safe_format(template_str, **kwargs):
                class SafeDict(dict):
                    def __missing__(self, key):
                        return "NA"
                return template_str.format_map(SafeDict(**kwargs))

            rendered_message = template.format_map(defaultdict(lambda: '[NA]', context)) # Упрощено

            def replace_tag_placeholders(match):
                tag_name = match.group(1)
                return context["tag"].get(tag_name, "[NA]")

            rendered_message = re.sub(r'\{tag\[(.*?)\]\}', replace_tag_placeholders, rendered_message)


            if len(rendered_message) > TELEGRAM_MAX_MESSAGE_LENGTH:
                rendered_message = rendered_message[:TELEGRAM_MAX_MESSAGE_LENGTH - 3] + "..."
            return rendered_message
        except KeyError as e:
            logger.error(f"TelegramRoutingIntegration: Missing key in template context: {e}")
            return f"Error rendering template: Missing data for {e}. Original message: {template}"
        except Exception as e:
            logger.error(f"TelegramRoutingIntegration: Error rendering template: {e}")
            return f"Error rendering template: {e}. Original message: {template}"

    def send_message(
        self,
        notification: Alert,
        event: Event,
        channel_id: str,
        config: dict[str, Any],
    ) -> None:
        api_token = config.get("api_token")
        receivers = config.get("receivers", "").split(";")
        template = config.get("template", self.get_config_data().get("default_message_template"))
        api_origin = self.get_config_data().get("api_origin", "https://api.telegram.org")

        if not api_token:
            logger.warning("TelegramRoutingIntegration: No API token configured for channel.")
            return

        if not receivers:
            logger.warning("TelegramRoutingIntegration: No receivers configured for channel.")
            return

        if not template:
            template = self.get_config_data().get("default_message_template")
            if not template:
                logger.error("TelegramRoutingIntegration: No message template found for channel or default.")
                return

        context = self.get_message_context(notification, event)
        message_text = self._render_message(template, context)

        headers = {"Content-Type": "application/json"}
        for chat_id in receivers:
            if not chat_id.strip():
                continue

            payload = {
                "chat_id": chat_id.strip(),
                "text": message_text,
                "parse_mode": "Markdown",
            }
            url = f"{api_origin}/bot{api_token}/sendMessage"

            try:
                response = safe_urlopen(url, headers=headers, data=json.dumps(payload))
                response.raise_for_status()
            except Exception as e:
                logger.error(f"TelegramRoutingIntegration: Failed to send message to chat_id {chat_id}: {e}")

    def get_notification_options(self, organization, user, integration_id) -> Sequence[NotificationOption]:
        return [
            NotificationOption(
                name="Telegram Routing Plus Notifications",
                description="Receive notifications from Sentry via Telegram.",
                flags=NotificationSetting.IssueAlert
            )
        ]

    def should_notify(self, notification: Alert, event: Event) -> bool:
        config_data = self.get_config_data()
        channels_config: TelegramChannelsConfigJson = json.loads(config_data.get("channels_config_json", '{"channels":[]}'))

        if not channels_config.get("channels"):
            logger.debug("No Telegram channels configured.")
            return False

        for channel in channels_config["channels"]:
            channel_filters = channel.get("filters", [])
            if not channel_filters:
                logger.debug(f"Channel {channel.get('receivers')} has no filters, acts as default.")
                return True

            if self._channel_matches_filters(event, channel_filters):
                logger.debug(f"Event matches filters for channel {channel.get('receivers')}.")
                return True

        logger.debug("Event does not match any Telegram channel filters.")
        return False

    def _channel_matches_filters(self, event: Event, filters: list[dict[str, Any]]) -> bool:
        for f in filters:
            filter_type = f.get("type")
            filter_value = f.get("value")

            if not filter_type or not filter_value:
                continue
            if filter_type == "regex__message":
                message = event.message or ""
                if not re.search(filter_value, message, re.IGNORECASE):
                    return False
            elif filter_type == "regex__title":
                title = event.title
                if not re.search(filter_value, title, re.IGNORECASE):
                    return False
            elif filter_type.startswith("tag__"):
                tag_name = filter_type.split("__", 1)[1]
                tag_value = event.tags.get(tag_name)
                if tag_value is None or not re.search(filter_value, tag_value, re.IGNORECASE):
                    return False
            elif filter_type == "level":
                if event.level != filter_value:
                    return False
            elif filter_type == "project_slug":
                if event.project and event.project.slug != filter_value:
                    return False
        return True

    def get_notification_settings_url(self):
        return None


class TelegramRoutingIntegrationProvider(IntegrationProvider):
    key = "telegram_routing_plus"
    name = "Telegram Routing Plus"
    metadata = IntegrationMetadata(
        description="Sentry Integration to route events to different Telegram channels based on custom rules.",
        features=[
            FeatureDescription(
                "Route Sentry alerts to different Telegram channels based on flexible JSON rules.",
                IntegrationFeatures.ALERT_RULE,
            )
        ],
        author="Boris Savinov",
        noun="Telegram",
        issue_url="https://gitlab.hellodoc.team/hellodoc/sentry-telegram-plus/-/issues",
        source_url="https://gitlab.hellodoc.team/hellodoc/sentry-telegram-plus",
        aspects={
            "supported_alerts": [
                "issue",
            ]
        },
    )

    integration_cls = TelegramRoutingIntegration
    features = frozenset([
        IntegrationFeatures.ALERT_RULE,
    ])


    def build_integration(self, state: Mapping[str, Any]) -> IntegrationData:

        config_form_data = state.get("form_data", {})
        return {
            "external_id": f"telegram_routing_plus_integration_{config_form_data.get('api_origin', '').split('//')[-1]}",
            "name": config_form_data.get("api_origin", "Telegram Routing Plus"),
            "metadata": {
                "api_origin": config_form_data.get("api_origin"),
                "channels_config_json": config_form_data.get("channels_config_json"),
                "default_message_template": config_form_data.get("default_message_template"),
            },
        }

    def setup(self):
        pass