# coding: utf-8
import re
import json
import logging
from collections import defaultdict
from typing import List

from django import forms
from django.utils.translation import gettext_lazy as _

from sentry.plugins.bases import notify
from sentry.http import safe_urlopen
from sentry.utils.safe import safe_execute

from . import __version__, __doc__ as package_doc


TELEGRAM_MAX_MESSAGE_LENGTH = 4096  # https://core.telegram.org/bots/api#sendmessage:~:text=be%20sent%2C%201%2D-,4096,-characters%20after%20entities
EVENT_TITLE_MAX_LENGTH = 500


class TelegramNotificationsOptionsForm(notify.NotificationConfigurationForm):
    api_origin = forms.CharField(
        label=_('Telegram API origin'),
        widget=forms.TextInput(attrs={'placeholder': 'https://api.telegram.org'}),
        initial='https://api.telegram.org'
    )
    channels_config_json = forms.CharField(
        label=_('Channels Configuration (JSON)'),
        widget=forms.Textarea(attrs={'class': 'span10', 'rows': 15}),
        help_text=_(
            'JSON configuration for routing messages to different channels. '
            'Example: {"channels": [{"api_token": "...", "receivers": "...", "template": "", "filters": [...]}]}')
    )
    default_message_template = forms.CharField(
        label=_('Default Message Template'),
        widget=forms.Textarea(attrs={'class': 'span4'}),
        help_text=_('Set in standard python\'s {}-format convention, available names are: '
                    '{project_name}, {url}, {title}, {message}, {tag[%your_tag%]}. Used if channel template is empty.'),
        initial='*[Sentry]* {project_name} {tag[level]}: *{title}*\n```\n{message}```\n{url}'
    )


class TelegramNotificationsPlugin(notify.NotificationPlugin):
    title = 'Telegram Notifications Plus'
    slug = 'sentry_telegram_plus'
    description = package_doc
    version = __version__
    author = 'Boris Savinov'
    author_url = 'https://gitlab.hellodoc.team/hellodoc/sentry-telegram-plus'
    resource_links = [
        ('Original version', 'https://github.com/butorov/sentry-telegram'),
        ('Source', 'https://github.com/butorov/sentry-telegram'),
    ]

    conf_key = 'sentry_telegram_plus'
    conf_title = title

    project_conf_form = TelegramNotificationsOptionsForm

    logger = logging.getLogger('sentry.plugins.sentry_telegram_plus')

    def is_configured(self, project, **kwargs):
        return bool(self.get_option('api_origin', project) and self.get_option('channels_config_json', project))

    def get_config(self, project, **kwargs):
        return [
            {
                'name': 'api_origin',
                'label': 'Telegram API origin',
                'type': 'text',
                'placeholder': 'https://api.telegram.org',
                'validators': [],
                'required': True,
                'default': 'https://api.telegram.org'
            },
            {
                'name': 'channels_config_json',
                'label': 'Channels Configuration (JSON)',
                'type': 'textarea',
                'help': 'JSON configuration for routing messages to different channels. '
                        'Example: {"api_origin": "https://api.telegram.org", '
                        '"channels": [{"api_token": "...", "receivers": "-123456789;2", "template": "", '
                        '"filters": [{"type":"regex__message", "value": "*"}, '
                        '{"type":"value__tag", "value": "pharma"}]}]}',
                'validators': [],
                'required': True,
            },
            {
                'name': 'default_message_template',
                'label': 'Default Message Template',
                'type': 'textarea',
                'help': 'Set in standard python\'s {}-format convention, available names are: '
                        '{project_name}, {url}, {title}, {message}, {tag[%your_tag%]}. Undefined tags will be shown as [NA]',
                'validators': [],
                'required': True,
                'default': '*[Sentry]* {project_name} {tag[level]}: *{title}*\n```{message}```\n{url}'
            },
        ]

    def compile_message_text(self, message_template: str, message_params: dict, event_message: str) -> str:
        """
        Compiles message text from template and event message.
        Truncates the original event message (`event.message`) to fit Telegram message length limit.
        """
        # TODO: add tests
        truncate_warning_text = '... (truncated)'
        truncate_warning_length = len(truncate_warning_text)

        truncated = False
        while True:
            message_text = message_template.format(**message_params, message=event_message)
            message_text_size = len(message_text)

            if truncated or message_text_size <= TELEGRAM_MAX_MESSAGE_LENGTH:
                break
            else:
                truncate_size = (message_text_size - TELEGRAM_MAX_MESSAGE_LENGTH) + truncate_warning_length
                event_message = event_message[:-truncate_size] + truncate_warning_text
                truncated = True

        return message_text

    def build_message(self, group, event, message_template):
        event_tags = defaultdict(lambda: '[NA]')
        event_tags.update({k: v for k, v in event.tags})

        message_params = {
            'title': event.title[:EVENT_TITLE_MAX_LENGTH],
            'tag': event_tags,
            'project_name': group.project.name,
            'url': group.get_absolute_url(),
        }
        text = self.compile_message_text(
            message_template,
            message_params,
            event.message,
        )

        return {
            'text': text,
            'parse_mode': 'Markdown',
        }

    def build_url(self, api_origin, api_token):
        return '%s/bot%s/sendMessage' % (api_origin, api_token)

    def get_message_template(self, project):
        return self.get_option('message_template', project)

    def get_receivers_list(self, receivers_str) -> List[List[str, str]]:
        if not receivers_str:
            return []
        return list([part.strip().split('/', maxsplit=1) for part in receivers_str.split(';') if part.strip()])

    def send_message(self, url, payload, receiver: List[str, str]):
        payload['chat_id'] = receiver[0]
        if len(receiver) > 1:
            payload['message_thread_id'] = receiver[1]
        self.logger.debug('Sending message to %s' % receiver)
        response = safe_urlopen(
            method='POST',
            url=url,
            json=payload,
        )
        self.logger.debug('Response code: %s, content: %s' % (response.status_code, response.content))
        if response.status_code > 299:
            raise ConnectionError(response.content)

    def _match_filter(self, event, filter_type, filter_value):
        if filter_type == "regex__message":
            return re.search(filter_value, event.message)
        elif filter_type == "value__tag":
            for tag_key, tag_value in event.tags:
                if tag_value == filter_value:
                    return True
            return False
        return False

    def _get_channels_config(self, project):
        try:
            config_json = self.get_option('channels_config_json', project)
            if config_json:
                config = json.loads(config_json)
                return config.get('channels', []), config.get('api_origin', self.get_option('api_origin', project))
        except json.JSONDecodeError:
            self.logger.error("Invalid JSON in channels_config_json for project %s", project.slug)
        return [], self.get_option('api_origin', project)

    def notify_users(self, group, event, fail_silently=False, **kwargs):
        self.logger.debug('Received notification for event: %s' % event)

        channels_config, global_api_origin = self._get_channels_config(group.project)
        default_template = self.get_option('default_message_template', group.project)

        matched_channel = None
        for channel in channels_config:
            filters = channel.get('filters', [])
            if not filters:
                continue

            all_filters_match = True
            for f in filters:
                filter_type = f.get('type')
                filter_value = f.get('value')
                if not self._match_filter(event, filter_type, filter_value):
                    all_filters_match = False
                    break

            if all_filters_match:
                matched_channel = channel
                break

        if not matched_channel:
            for channel in channels_config:
                if not channel.get('filters'):
                    matched_channel = channel
                    break

        if matched_channel:
            api_token = matched_channel.get('api_token')
            receivers_str = matched_channel.get('receivers')
            channel_template = matched_channel.get('template') or default_template
            api_origin = matched_channel.get('api_origin', global_api_origin)

            if not api_token or not receivers_str:
                self.logger.warning(
                    f"Matched channel is missing api_token or receivers for project {group.project.slug}"
                )
                return

            receivers = self.get_receivers_list(receivers_str)
            self.logger.debug('for receivers: %s' % ', '.join(['/'.join(item) for item in receivers] or ()))

            payload = self.build_message(group, event, channel_template)
            self.logger.debug('Built payload: %s' % payload)

            url = self.build_url(api_origin, api_token)
            self.logger.debug('Built url: %s' % url)

            for receiver in receivers:
                safe_execute(self.send_message, url, payload, receiver,_with_transaction=False)
        else:
            self.logger.info("No matching channel found for event in project %s. Event not sent.", group.project.slug)
