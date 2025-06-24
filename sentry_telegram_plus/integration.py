import logging
import json
import re
from collections import defaultdict

from django import forms
from django.utils.translation import gettext_lazy as _

from sentry.integrations.models.integration import Integration 
from sentry.integrations.base import IntegrationFeatures, IntegrationProvider
from sentry.integrations.base import IntegrationConfig

from sentry.integrations.notifications import NotificationConfigurationProvider
from sentry.integrations.settings import IntegrationOption  # Для полей конфигурации
from sentry.utils.safe import safe_execute
from sentry.http import safe_urlopen

# Возможно, вам потребуется явно указать относительный путь для импорта __version__
# Если __init__.py находится в той же директории, это сработает.
from . import __version__, __doc__ as package_doc

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
EVENT_TITLE_MAX_LENGTH = 500

logger = logging.getLogger('sentry.integrations.telegram_routing')  # Используем новый логгер для интеграции


# --- 1. Класс конфигурации интеграции ---
# Этот класс определяет поля, которые будут доступны пользователю для настройки интеграции.
# Это аналог вашей старой TelegramNotificationsOptionsForm, но для интеграций.
class TelegramRoutingIntegrationConfig(IntegrationConfig):
    api_origin = forms.CharField(
        label=_('Telegram API origin'),
        widget=forms.TextInput(attrs={'placeholder': 'https://api.telegram.org'}),
        initial='https://api.telegram.org',
        help_text=_('The base URL for the Telegram Bot API. Defaults to https://api.telegram.org.')
    )
    channels_config_json = forms.CharField(
        label=_('Channels Configuration (JSON)'),
        widget=forms.Textarea(attrs={'class': 'span10', 'rows': 15}),
        help_text=_(
            'JSON configuration for routing messages to different Telegram channels. '
            'Each channel can have its own API token, receivers, message template, and filters. '
            'If no filters are specified for a channel, it acts as a default fallback. '
            'Example: <pre>{&quot;api_origin&quot;: &quot;https://api.telegram.org&quot;, &quot;channels&quot;: [{&quot;api_token&quot;: &quot;YOUR_BOT_TOKEN&quot;, &quot;receivers&quot;: &quot;-123456789;2&quot;, &quot;template&quot;: &quot;&quot;, &quot;filters&quot;: [{&quot;type&quot;:&quot;regex__message&quot;, &quot;value&quot;: &quot;.*error.*&quot;}]}]}</pre>'
            # Используем HTML-сущности для <pre> и " внутри help_text
        ),
        required=True
    )
    default_message_template = forms.CharField(
        label=_('Default Message Template'),
        widget=forms.Textarea(attrs={'class': 'span4'}),
        help_text=_('Set in standard Python\'s {}-format convention. '
                    'Available names are: {project_name}, {url}, {title}, {message}, {tag[%your_tag%]}. '
                    'Undefined tags will be shown as [NA]. This template is used if a specific channel template is empty.'),
        initial='*[Sentry]* {project_name} {tag[level]}: *{title}*\n```\n{message}```\n{url}',
        required=True
    )

    def clean(self):
        """
        Дополнительная валидация JSON-конфигурации каналов.
        """
        cleaned_data = super().clean()
        channels_config_json = cleaned_data.get('channels_config_json')
        if channels_config_json:
            try:
                config = json.loads(channels_config_json)
                # Проверяем, что 'channels' является списком
                if 'channels' not in config or not isinstance(config['channels'], list):
                    raise forms.ValidationError(
                        _("Channels configuration must contain a 'channels' key with a list of channel objects.")
                    )
                # Можно добавить более глубокую валидацию структуры каналов здесь
            except json.JSONDecodeError as e:
                raise forms.ValidationError(
                    _("Invalid JSON in Channels Configuration: %s. Please check your syntax.") % e
                )
        return cleaned_data


# --- 2. Класс провайдера уведомлений ---
# Этот класс отвечает за фактическую отправку уведомлений.
# Он содержит логику, которая была в notify_users вашего старого плагина.
class TelegramRoutingNotificationProvider(NotificationConfigurationProvider):
    # Уникальный идентификатор для этого провайдера уведомлений.
    # Используется в правилах оповещений Sentry.
    id = "sentry_telegram_routing_notification_provider"
    name = "Telegram Routing"  # Отображаемое имя в UI правил оповещений
    supported_features = {
        IntegrationFeatures.ALERT_RULE: True,  # Указывает, что интеграция может использоваться в правилах оповещений
    }

    # Вспомогательные методы (перенесены из старого плагина)
    def compile_message_text(self, message_template: str, message_params: dict, event_message: str) -> str:
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

    def get_receivers_list(self, receivers_str) -> list[list[str, str]]:
        if not receivers_str:
            return []
        return list([part.strip().split('/', maxsplit=1) for part in receivers_str.split(';') if part.strip()])

    def send_message(self, url, payload, receiver: list[str, str]):
        payload['chat_id'] = receiver[0]
        if len(receiver) > 1:
            payload['message_thread_id'] = receiver[1]
        logger.debug('Sending message to %s', receiver)
        response = safe_urlopen(
            method='POST',
            url=url,
            json=payload,
        )
        logger.debug('Response code: %s, content: %s', response.status_code, response.content)
        if response.status_code > 299:
            raise ConnectionError(response.content)

    def _match_filter(self, event, filter_type, filter_value):
        if filter_type == "regex__message":
            # Используем re.search для поиска подстроки по регулярному выражению в сообщении
            # event.message может быть None, поэтому добавляем or ''
            return re.search(filter_value, event.message or '')
        elif filter_type == "value__tag":
            # Проверяем наличие тега с указанным значением
            for tag_key, tag_value in event.tags:
                if tag_value == filter_value:
                    return True
            return False
        return False  # Неизвестный тип фильтра

    def _get_channels_config(self, integration):
        """
        Получает и парсит JSON-конфигурацию каналов из IntegrationConfig.
        """
        try:
            config_json = integration.get_config_option('channels_config_json')
            if config_json:
                config = json.loads(config_json)
                # Возвращаем список каналов и api_origin из верхнего уровня JSON, если есть,
                # иначе используем api_origin из настроек интеграции
                return config.get('channels', []), config.get('api_origin', integration.get_config_option('api_origin'))
        except json.JSONDecodeError:
            # Логируем ошибку, но не прерываем работу, возвращаем дефолтные значения
            logger.error("Invalid JSON in channels_config_json for integration %s. Please check your configuration.",
                         integration.id)
        return [], integration.get_config_option('api_origin')

    # --- Ключевой метод отправки уведомлений для интеграций ---
    def notify(self, notification, event, **kwargs):
        """
        Отправляет уведомление на основе события и настроек интеграции.
        """
        group = event.group
        # notification.integration предоставляет доступ к объекту Integration и его конфигурации
        integration = notification.integration

        logger.debug('Received notification for event: %s via integration %s', event.event_id, integration.id)

        # Получаем конфигурацию каналов и дефолтный шаблон из настроек интеграции
        channels_config, global_api_origin = self._get_channels_config(integration)
        default_template = integration.get_config_option('default_message_template')

        matched_channel = None
        default_channel = None  # Для хранения канала без фильтров (общий канал)

        # 1. Поиск подходящего канала с фильтрами
        for channel in channels_config:
            filters = channel.get('filters', [])
            if not filters:
                # Если фильтров нет, это потенциальный общий канал, сохраняем его на потом
                default_channel = channel
                continue

            all_filters_match = True
            for f in filters:
                filter_type = f.get('type')
                filter_value = f.get('value')
                if not self._match_filter(event, filter_type, filter_value):
                    all_filters_match = False
                    break  # Если хотя бы один фильтр не совпал, этот канал не подходит

            if all_filters_match:
                matched_channel = channel
                break  # Найден подходящий канал, прекращаем поиск

        # 2. Если ни один канал с фильтрами не совпал, используем общий канал, если он есть
        if not matched_channel:
            matched_channel = default_channel

        if matched_channel:
            api_token = matched_channel.get('api_token')
            receivers_str = matched_channel.get('receivers')
            # Используем шаблон канала, если он есть, иначе общий шаблон
            channel_template = matched_channel.get('template') or default_template
            # Приоритет API origin: сначала из канала, затем из верхнего уровня JSON, затем из настроек Sentry
            api_origin = matched_channel.get('api_origin', global_api_origin)

            if not api_token or not receivers_str:
                logger.warning(
                    "Matched channel is missing api_token or receivers for integration %s. Event not sent to this channel.",
                    integration.id
                )
                return

            receivers = self.get_receivers_list(receivers_str)
            logger.debug('Sending to receivers: %s', ', '.join(['/'.join(item) for item in receivers] or ()))

            payload = self.build_message(group, event, channel_template)
            logger.debug('Built payload: %s', payload)

            url = self.build_url(api_origin, api_token)
            logger.debug('Built URL: %s', url)

            for receiver in receivers:
                # Используем _with_transaction=False для Sentry 22+
                safe_execute(self.send_message, url, payload, receiver, _with_transaction=False)
        else:
            logger.info(
                "No matching or default channel found for event '%s' in project '%s' via integration '%s'. Event not sent.",
                event.event_id, group.project.slug, integration.id
            )


# --- 3. Класс самой интеграции ---
# Этот класс представляет вашу интеграцию в Sentry.
class TelegramRoutingIntegration(Integration):
    id = "telegram_routing_plus"  # Уникальный ID для вашей интеграции (slug)
    name = "Telegram Notifications Plus"  # Отображаемое имя интеграции в Sentry UI
    icon = "https://www.telegram.org/favicon.ico"  # Путь к иконке (может быть локальным или внешним)
    description = package_doc  # Используем описание из __init__.py
    features = [
        IntegrationFeatures.ALERT_RULE,  # Указываем, что интеграция поддерживает правила оповещений
    ]

    # Связываем форму конфигурации с этой интеграцией
    config_form = TelegramRoutingIntegrationConfig

    # Связываем провайдера уведомлений с этой интеграцией
    notification_configuration_provider = TelegramRoutingNotificationProvider

    # Определяем поля конфигурации, которые будут храниться в базе данных Sentry
    # и доступны через integration.get_config_option()
    _options = [
        IntegrationOption("api_origin", default="https://api.telegram.org", required=True),
        IntegrationOption("channels_config_json", required=True),
        IntegrationOption("default_message_template",
                          default='*[Sentry]* {project_name} {tag[level]}: *{title}*\n```\n{message}```\n{url}',
                          required=True),
    ]

    def get_config_options(self) -> list[IntegrationOption]:
        """
        Возвращает список опций конфигурации для этой интеграции.
        """
        return self._options

    def get_installation_url(self):
        """
        Для локальных интеграций это может быть None или заглушка,
        поскольку нет внешнего URL для установки.
        """
        return None

    # Если вы хотите, чтобы ваш плагин по-прежнему отображался как "плагин"
    # (хотя он уже интеграция), вы можете определить этот метод.
    # Но для новой интеграции обычно это не нужно, если вы полностью перешли.
    # def get_plugin_features(self) -> list[IntegrationFeature]:
    #     return []