import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application
from django.urls import path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "findit.settings")
django_asgi_app = get_asgi_application()

from core.consumers import ConversationConsumer, NotificationConsumer

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(
            AuthMiddlewareStack(
                URLRouter(
                    [
                        path("ws/conversations/<uuid:conversation_id>/", ConversationConsumer.as_asgi()),
                        path("ws/notifications/", NotificationConsumer.as_asgi()),
                    ]
                )
            )
        ),
    }
)
