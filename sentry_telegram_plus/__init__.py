"""
Sentry интеграция (плагин) для перенаправления евентов в различные телеграм каналы, в зависимости от
различных правил.
"""

__version__ = '0.6.1'

from .integration import TelegramRoutingIntegration, TelegramRoutingIntegrationProvider # noqa
