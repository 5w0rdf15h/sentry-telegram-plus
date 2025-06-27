# coding: utf-8
import json
import logging
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, TypedDict, Union, Tuple

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from sentry.http import safe_urlopen
from sentry.plugins.bases import notify
from sentry.utils.safe import safe_execute

from . import __doc__ as package_doc
from . import __version__

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
EVENT_TITLE_MAX_LENGTH = 500

logger = logging.getLogger("sentry.plugins.sentry_telegram_plus")


class ChannelFilter(TypedDict):
    type: str
    value: str


class ChannelConfig(TypedDict, total=False):
    api_token: str
    receivers: str
    template: Optional[str]
    api_origin: Optional[str]
    filters: List[ChannelFilter]


class ChannelsConfigJson(TypedDict):
    api_origin: Optional[str]
    channels: List[ChannelConfig]


class TelegramNotificationsOptionsForm(notify.NotificationConfigurationForm):
    api_origin = forms.CharField(
        label=_("Telegram API origin"),
        widget=forms.TextInput(attrs={"placeholder": "https://api.telegram.org"}),
        initial="https://api.telegram.org",
        help_text=_(
            "The base URL for the Telegram Bot API. Defaults to https://api.telegram.org."
        ),
    )
    channels_config_json = forms.CharField(
        label=_("Channels Configuration (JSON)"),
        widget=forms.Textarea(attrs={"class": "span10", "rows": 15}),
        help_text=_(
            "JSON configuration for routing messages to different channels. "
            "Each channel can have its own API token, receivers, message template, and filters. "
            "If no filters are specified for a channel, it acts as a default fallback. "
            "Example: <pre>{&quot;api_origin&quot;: &quot;https://api.telegram.org&quot;, &quot;channels&quot;: [{&quot;api_token&quot;: &quot;YOUR_BOT_TOKEN&quot;, &quot;receivers&quot;: &quot;-123456789;2&quot;, &quot;template&quot;: &quot;&quot;, &quot;filters&quot;: [{&quot;type&quot;:&quot;regex__message&quot;, &quot;value&quot;: &quot;.*error.*&quot;}]}]}</pre>"
        ),
        required=True,
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
        required=True,
    )

    def clean_api_origin(self):
        value = self.cleaned_data["api_origin"]
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValidationError(
                _(
                    "Telegram API origin must be a valid URL starting with http:// or https://."
                )
            )
        return value

    def clean_channels_config_json(self):
        config_json = self.cleaned_data["channels_config_json"]
        try:
            config: ChannelsConfigJson = json.loads(config_json)
            if "channels" not in config or not isinstance(config["channels"], list):
                raise ValidationError(
                    _(
                        "Channels configuration must contain a 'channels' key with a list of channel objects."
                    )
                )
            for i, channel in enumerate(config["channels"]):
                if not isinstance(channel, dict):
                    raise ValidationError(
                        _(f"Channel at index {i} must be a dictionary.")
                    )
                if "api_token" not in channel or not channel["api_token"]:
                    raise ValidationError(
                        _(f"Channel at index {i} must have a non-empty 'api_token'.")
                    )
                if "receivers" not in channel or not channel["receivers"]:
                    raise ValidationError(
                        _(f"Channel at index {i} must have a non-empty 'receivers'.")
                    )

                filters = channel.get("filters", [])
                if not isinstance(filters, list):
                    raise ValidationError(
                        _(f"Filters for channel at index {i} must be a list.")
                    )
                for j, f in enumerate(filters):
                    if not isinstance(f, dict) or "type" not in f or "value" not in f:
                        raise ValidationError(
                            _(
                                f"Filter at channel index {i}, filter index {j} must be a dictionary with 'type' and 'value'."
                            )
                        )

        except json.JSONDecodeError as e:
            raise ValidationError(
                _(
                    "Invalid JSON in Channels Configuration: %s. Please check your syntax."
                )
                % e
            )
        except ValidationError as e:
            raise e
        except Exception as e:
            raise ValidationError(
                _(f"An unexpected error occurred during JSON validation: {e}")
            )
        return config_json


class TelegramNotificationsPlugin(notify.NotificationPlugin):
    title = "Telegram Notifications Plus"
    slug = "sentry_telegram_plus"
    description = package_doc
    version = __version__
    author = "Boris Savinov"
    author_url = "https://gitlab.hellodoc.team/hellodoc/sentry-telegram-plus"
    resource_links = [
        ("Original version", "https://github.com/butorov/sentry-telegram"),
        (
            "Hello, Doc Repo",
            "https://gitlab.hellodoc.team/hellodoc/sentry-telegram-plus",
        ),
    ]

    conf_key = "sentry_telegram_plus"
    conf_title = title

    project_conf_form = TelegramNotificationsOptionsForm

    def is_configured(self, project, **kwargs) -> bool:
        """Проверяет, настроен ли плагин для проекта."""
        return bool(self.get_option('api_origin', project) and self.get_option('channels_config_json', project))

    def get_config(self, project, **kwargs) -> List[Dict[str, Any]]:
        """
        Возвращает конфигурацию полей для UI Sentry.
        Этот метод используется Sentry для динамического построения формы.
        """
        return [
            {
                'name': 'api_origin',
                'label': _('Telegram API origin'),
                'type': 'text',
                'placeholder': 'https://api.telegram.org',
                'validators': [],  # Validators defined in form clean methods
                'required': True,
                'default': 'https://api.telegram.org',
                'help': _('The base URL for the Telegram Bot API. Defaults to https://api.telegram.org.')
            },
            {
                'name': 'channels_config_json',
                'label': _('Channels Configuration (JSON)'),
                'type': 'textarea',
                'help': _(
                    'JSON configuration for routing messages to different channels. '
                    'Each channel can have its own API token, receivers, message template, and filters. '
                    'If no filters are specified for a channel, it acts as a default fallback. '
                    'Example: <pre>{&quot;api_origin&quot;: &quot;https://api.telegram.org&quot;, &quot;channels&quot;: [{&quot;api_token&quot;: &quot;YOUR_BOT_TOKEN&quot;, &quot;receivers&quot;: &quot;-123456789;2&quot;, &quot;template&quot;: &quot;&quot;, &quot;filters&quot;: [{&quot;type&quot;:&quot;regex__message&quot;, &quot;value&quot;: &quot;.*error.*&quot;}]}]}</pre>'
                ),
                'validators': [],
                'required': True,
            },
            {
                'name': 'default_message_template',
                'label': _('Default Message Template'),
                'type': 'textarea',
                'help': _('Set in standard Python\'s {}-format convention. '
                              'Available names are: {project_name}, {url}, {title}, {message}, {tag[%your_tag%]}. '
                              'Undefined tags will be shown as [NA]. This template is used if a specific channel template is empty.'),
                'validators': [],
                'required': True,
                'default': '*[Sentry]* {project_name} {tag[level]}: *{title}*\n```\n{message}```\n{url}'
            },
        ]

    def compile_message_text(
        self, message_template: str, message_params: Dict[str, Any], event_message: str
    ) -> str:
        """
        Собирает текст сообщения из шаблона и данных события, усекая его, если необходимо.
        """
        truncate_warning_text = "... (truncated)"
        # Максимальная длина сообщения с учетом предупреждения
        max_message_body_len = TELEGRAM_MAX_MESSAGE_LENGTH - len(
            message_template.format(**message_params, message=truncate_warning_text)
        )
        if max_message_body_len < 0:
            max_message_body_len = 0

        if len(event_message) > max_message_body_len:
            event_message = event_message[:max_message_body_len] + truncate_warning_text

        return message_template.format(**message_params, message=event_message)

    def build_message(self, group, event, message_template: str) -> Dict[str, Any]:
        """Создание сообщения для отправки в Telegram."""
        event_tags = defaultdict(lambda: "[NA]")
        event_tags.update({k: v for k, v in event.tags})

        message_params = {
            "title": event.title[:EVENT_TITLE_MAX_LENGTH],
            "tag": event_tags,
            "project_name": group.project.name,
            "url": group.get_absolute_url(),
        }
        text = self.compile_message_text(
            message_template,
            message_params,
            event.message or "",
        )

        return {
            "text": text,
            "parse_mode": "Markdown",
        }

    def build_url(self, api_origin: str, api_token: str) -> str:
        return f"{api_origin}/bot{api_token}/sendMessage"

    def get_receivers_list(self, receivers_str: str) -> List[List[str]]:
        """Парсит строку получателей в список списков [chat_id, message_thread_id]."""
        if not receivers_str:
            return []
        parsed_receivers: List[List[str]] = []
        for part in receivers_str.split(";"):
            stripped_part = part.strip()
            if stripped_part:
                parsed_receivers.append(stripped_part.split("/", maxsplit=1))
        return parsed_receivers

    def send_message(self, url: str, payload: Dict[str, Any], receiver: List[str]):
        """Отправляет сообщение одному получателю Telegram."""
        chat_id = receiver[0]
        payload_copy = payload.copy()
        payload_copy["chat_id"] = chat_id
        if len(receiver) > 1:
            payload_copy["message_thread_id"] = receiver[1]

        logger.debug("Sending message to %s" % receiver)
        try:
            response = safe_urlopen(
                method="POST",
                url=url,
                json=payload_copy,
            )
            response.raise_for_status()
            logger.debug(
                "Response code: %s, content: %s"
                % (response.status_code, response.content)
            )
        except Exception as e:
            logger.error(
                f"Failed to send message to chat_id {chat_id}: {e}", exc_info=True
            )

    def _match_filter(self, event: Any, filter_type: str, filter_value: str) -> bool:
        """Проверяет, соответствует ли событие заданному фильтру."""
        if filter_type == "regex__message":
            return bool(re.search(filter_value, event.message or "", re.IGNORECASE))
        elif filter_type == "regex__title":
            return bool(re.search(filter_value, event.title or "", re.IGNORECASE))
        elif filter_type.startswith("tag__"):
            tag_name = filter_type.split("__", 1)[1]
            tag_value = dict(event.tags).get(tag_name)
            return bool(tag_value and re.search(filter_value, tag_value, re.IGNORECASE))
        elif filter_type == "level":
            return event.level == filter_value
        elif filter_type == "project_slug":
            return event.project and event.project.slug == filter_value
        elif filter_type == "value__tag":
            for tag_key, tag_value in event.tags:
                if tag_value == filter_value:
                    return True
            return False
        logger.warning(f"Не поддерживаемый тип фильтра: {filter_type}")
        return False

    def _get_channels_config_data(self, project) -> Tuple[List[ChannelConfig], str]:
        """Получает и парсит конфигурацию каналов из настроек проекта."""
        try:
            config_json = self.get_option("channels_config_json", project)
            if config_json:
                config: ChannelsConfigJson = json.loads(config_json)
                return config.get("channels", []), config.get(
                    "api_origin", self.get_option("api_origin", project)
                )
        except json.JSONDecodeError:
            logger.error(
                "Invalid JSON in channels_config_json for project %s during runtime, this should have been caught by form validation.",
                project.slug,
            )
        except Exception as e:
            logger.error(
                f"Unexpected error loading channels config for project {project.slug}: {e}",
                exc_info=True,
            )

        return [], self.get_option("api_origin", project)

    def _find_matching_channel(
        self, event: Any, channels_config: List[ChannelConfig]
    ) -> Optional[ChannelConfig]:
        """Находит первый канал, соответствующий фильтрам события, или дефолтный канал без фильтров."""
        matched_channel = None
        default_channel = None

        for channel in channels_config:
            filters = channel.get("filters", [])
            if not filters:
                default_channel = channel
                continue

            all_filters_match = True
            for f in filters:
                filter_type = f.get("type")
                filter_value = f.get("value")
                if (
                    not filter_type
                    or not filter_value
                    or not self._match_filter(event, filter_type, filter_value)
                ):
                    all_filters_match = False
                    break

            if all_filters_match:
                matched_channel = channel
                break

        return matched_channel or default_channel

    def notify_users(self, group, event, fail_silently=False, **kwargs) -> None:
        """Основной метод для отправки уведомлений."""
        logger.debug("Received notification for event: %s" % event)

        channels_config, global_api_origin = self._get_channels_config_data(
            group.project
        )
        default_template = self.get_option("default_message_template", group.project)

        if not channels_config:
            logger.info(
                "No Telegram channels configured for project %s. Event not sent.",
                group.project.slug,
            )
            return

        matched_channel = self._find_matching_channel(event, channels_config)

        if not matched_channel:
            logger.info(
                "No matching channel or default channel found for event in project %s. Event not sent.",
                group.project.slug,
            )
            return

        api_token = matched_channel.get("api_token")
        receivers_str = matched_channel.get("receivers")
        channel_template = matched_channel.get("template") or default_template
        api_origin = matched_channel.get("api_origin", global_api_origin)

        if not api_token or not receivers_str:
            logger.warning(
                f"Matched channel is missing api_token or receivers for project {group.project.slug}. Notification skipped."
            )
            return

        receivers = self.get_receivers_list(receivers_str)
        if not receivers:
            logger.warning(
                f"No valid receivers parsed for matched channel in project {group.project.slug}. Notification skipped."
            )
            return

        logger.debug(
            "for receivers: %s"
            % ", ".join(["/".join(item) for item in receivers] or ())
        )

        payload = self.build_message(group, event, channel_template)
        logger.debug("Built payload: %s" % payload)

        url = self.build_url(api_origin, api_token)
        logger.debug("Built url: %s" % url)

        for receiver in receivers:
            safe_execute(
                self.send_message, url, payload, receiver, _with_transaction=False
            )
