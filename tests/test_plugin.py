import pytest
import json
import re
from unittest.mock import patch, MagicMock
from conftest import MockEvent, MockProject

from sentry_telegram_plus.plugin import (
    TelegramNotificationsPlugin,
    TelegramNotificationsOptionsForm,
    ValidationError,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    EVENT_TITLE_MAX_LENGTH,
)


class TestTelegramNotificationsPluginLogic:
    def _set_plugin_config(self, plugin_instance, project_mock, channels_config):
        default_api_origin = channels_config.get('api_origin', 'https://api.telegram.org')
        plugin_instance.set_option('api_origin', default_api_origin, project=project_mock)
        plugin_instance.set_option(
            'channels_config_json',
            json.dumps(channels_config),
            project=project_mock
        )
        plugin_instance.set_option(
            'default_message_template',
            "*[Sentry]* {project_name} {tag[level]}: *{title}*\n```\n{message}```\n{url}",
            project=project_mock
        )

    def test_notify_no_filters_all_channels_sent(self, plugin_and_project):
        plugin, project_mock = plugin_and_project
        config = {
            "api_origin": "https://api.telegram.org",
            "channels": [
                {"api_token": "token1", "receivers": "chat1", "template": "Test 1: {message}"},
                {"api_token": "token2", "receivers": "chat2", "template": "Test 2: {message}"},
            ]
        }
        self._set_plugin_config(plugin, project_mock, config)
        event_mock = MockEvent(message="Hello from Sentry!")

        with patch.object(plugin, 'send_message') as mock_send_message:
            plugin.notify_users(group=event_mock.group, event=event_mock)

            assert mock_send_message.call_count == 2

            call_args_1 = mock_send_message.call_args_list[0].kwargs
            assert call_args_1['url'] == f"{config['api_origin']}/bot{config['channels'][0]['api_token']}/sendMessage"
            assert call_args_1['payload']['chat_id'] == 'chat1'
            assert call_args_1['payload']['text'] == "Test 1: Hello from Sentry!"
            assert call_args_1['receiver'] == ['chat1']

            call_args_2 = mock_send_message.call_args_list[1].kwargs
            assert call_args_2['url'] == f"{config['api_origin']}/bot{config['channels'][1]['api_token']}/sendMessage"
            assert call_args_2['payload']['chat_id'] == 'chat2'
            assert call_args_2['payload']['text'] == "Test 2: Hello from Sentry!"
            assert call_args_2['receiver'] == ['chat2']

    def test_notify_matching_filter_sends_to_filtered_channel(self, plugin_and_project):
        plugin, project_mock = plugin_and_project
        config = {
            "api_origin": "https://api.telegram.org",
            "channels": [
                {"api_token": "token_filtered", "receivers": "chat_filtered",
                 "filters": [{"type": "regex__message", "value": ".*critical.*"}], "template": "Filtered: {message}"},
                {"api_token": "token_default", "receivers": "chat_default", "template": "Default: {message}"},
            ]
        }
        self._set_plugin_config(plugin, project_mock, config)
        event_mock = MockEvent(message="Something critical happened!", level="fatal")

        with patch.object(plugin, 'send_message') as mock_send_message:
            plugin.notify_users(group=event_mock.group, event=event_mock)

            assert mock_send_message.call_count == 1
            call_args = mock_send_message.call_args_list[0].kwargs
            assert call_args['payload']['chat_id'] == config['channels'][0]['receivers']
            assert call_args['payload']['text'] == "Filtered: Something critical happened!"

    def test_notify_non_matching_filter_skips_channel(self, plugin_and_project):
        plugin, project_mock = plugin_and_project
        config = {
            "api_origin": "https://api.telegram.org",
            "channels": [
                {"api_token": "token_filtered", "receivers": "chat_filtered",
                 "filters": [{"type": "regex__message", "value": ".*critical.*"}], "template": "Filtered: {message}"},
                {"api_token": "token_default", "receivers": "chat_default", "template": "Default: {message}"},
            ]
        }
        self._set_plugin_config(plugin, project_mock, config)
        event_mock = MockEvent(message="Just a warning.", level="warning")  # Does not match 'critical'

        with patch.object(plugin, 'send_message') as mock_send_message:
            plugin.notify_users(group=event_mock.group, event=event_mock)

            assert mock_send_message.call_count == 1
            call_args = mock_send_message.call_args_list[0].kwargs
            assert call_args['payload']['chat_id'] == config['channels'][1]['receivers']
            assert call_args['payload']['text'] == "Default: Just a warning."

    def test_send_message_with_message_thread_id(self, plugin_and_project):
        plugin, project_mock = plugin_and_project
        receiver_with_topic = ["12345", "678"]
        api_token = "mock:token"
        message_content = "Hello with topic!"
        api_origin = "https://api.telegram.org"

        url = plugin.build_url(api_origin, api_token)
        payload = plugin.build_message(
            group=MagicMock(project=project_mock, get_absolute_url=MagicMock(return_value="http://mock.url")),
            event=MockEvent(message=message_content, title="Test Title"),
            message_template="{message}"
        )

        with patch('sentry_telegram_plus.plugin.safe_urlopen') as mock_safe_urlopen:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.raise_for_status.return_value = None
            mock_safe_urlopen.return_value = mock_response

            plugin.send_message(url=url, payload=payload, receiver=receiver_with_topic)

            expected_chat_id = "12345"
            expected_message_thread_id = "678"

            mock_safe_urlopen.assert_called_once_with(
                method="POST",
                url=url,
                json={
                    'chat_id': expected_chat_id,
                    'text': message_content,
                    'parse_mode': 'Markdown',
                    'message_thread_id': expected_message_thread_id
                },
            )
            mock_response.raise_for_status.assert_called_once()

    def test_get_receivers_single(self, plugin_and_project):
        plugin, _ = plugin_and_project
        assert plugin.get_receivers_list('chat1') == [['chat1']]

    def test_get_receivers_multiple(self, plugin_and_project):
        plugin, _ = plugin_and_project
        assert plugin.get_receivers_list('chat1;chat2') == [['chat1'], ['chat2']]

    def test_get_receivers_with_topics(self, plugin_and_project):
        plugin, _ = plugin_and_project
        assert plugin.get_receivers_list('chat1/123;chat2/456') == [['chat1', '123'], ['chat2', '456']]

    def test_get_receivers_mixed(self, plugin_and_project):
        plugin, _ = plugin_and_project
        assert plugin.get_receivers_list('chat1;chat2/456') == [['chat1'], ['chat2', '456']]

    def test_get_receivers_empty(self, plugin_and_project):
        plugin, _ = plugin_and_project
        assert plugin.get_receivers_list('') == []
        assert plugin.get_receivers_list(None) == []

    def test_is_configured_true(self, plugin_and_project):
        plugin, project_mock = plugin_and_project
        # Set minimal config for it to be configured
        plugin.set_option('api_origin', 'https://api.telegram.org', project=project_mock)
        plugin.set_option('channels_config_json', json.dumps({"channels": [{"api_token": "t", "receivers": "r"}]}),
                          project=project_mock)
        assert plugin.is_configured(project_mock) is True

    def test_is_configured_false_no_api_origin(self, plugin_and_project):
        plugin, project_mock = plugin_and_project
        plugin.set_option('channels_config_json', json.dumps({"channels": [{"api_token": "t", "receivers": "r"}]}),
                          project=project_mock)
        assert plugin.is_configured(project_mock) is False

    def test_is_configured_false_no_channels_config(self, plugin_and_project):
        plugin, project_mock = plugin_and_project
        plugin.set_option('api_origin', 'https://api.telegram.org', project=project_mock)
        assert plugin.is_configured(project_mock) is False


    def test_find_matching_channel_no_match_returns_none(self, plugin_and_project):
        plugin, project_mock = plugin_and_project
        channels_config = {
            "channels": [
                {"api_token": "token1", "receivers": "chat1",
                 "filters": [{"type": "regex__message", "value": ".*error.*"}]},
            ]
        }
        event = MockEvent(message="Just some info.")

        matching_channel = plugin._find_matching_channel(channels_config["channels"], event)
        assert matching_channel is None

    def test_find_matching_channel_no_filters_matches_first(self, plugin_and_project):
        plugin, project_mock = plugin_and_project
        channels_config = {
            "channels": [
                {"api_token": "token1", "receivers": "chat1"},  # No filters
                {"api_token": "token2", "receivers": "chat2",
                 "filters": [{"type": "regex__message", "value": ".*error.*"}]},
            ]
        }
        event = MockEvent(message="Any message.")

        matching_channel = plugin._find_matching_channel(channels_config["channels"], event)
        assert matching_channel == channels_config["channels"][0]  # Should pick the first one without filters

    def test_find_matching_channel_channel_with_empty_filters_is_default(self, plugin_and_project):
        plugin, project_mock = plugin_and_project
        channels_config = {
            "channels": [
                {"api_token": "token1", "receivers": "chat1", "filters": []},  # Empty filters list
                {"api_token": "token2", "receivers": "chat2",
                 "filters": [{"type": "regex__message", "value": ".*error.*"}]},
            ]
        }
        event = MockEvent(message="Any message.")

        matching_channel = plugin._find_matching_channel(channels_config["channels"], event)
        assert matching_channel == channels_config["channels"][0]
