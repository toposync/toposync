"""Automatic capability metadata and pure preparation for the existing notification sink."""

from .connection import resolve_home_assistant_servers
from .pipelines import HomeAssistantNotifyConfig, _parse_notify_service


def register_automatic_capabilities(services):
    services.register(
        "automatic.catalog.home_assistant",
        lambda: [
            {
                "id": "home_assistant.notify",
                "kind": "consequence",
                "extension_id": "com.toposync.home_assistant",
                "title": {
                    "en": "Notify through Home Assistant",
                    "pt-BR": "Avisar pelo Home Assistant",
                },
                "operators": ["home_assistant.notify"],
                "planner": "automatic.plan.home_assistant.notify",
                "required_consents": ["external"],
                "parameters_schema": HomeAssistantNotifyConfig.model_json_schema(),
                "fields": [
                    {"name": key, "type": "text", "title": {"en": en, "pt-BR": pt}}
                    for key, en, pt in [
                        ("server_id", "Configured server", "Servidor configurado"),
                        ("notify_service", "Notification destination", "Destino do aviso"),
                        ("title", "Title", "Título"),
                        ("message", "Message", "Mensagem"),
                    ]
                ],
            }
        ],
    )
    services.register("automatic.plan.home_assistant.notify", plan_notification)


def plan_notification(*, consequence, configuration):
    parsed = HomeAssistantNotifyConfig.model_validate(consequence.parameters)
    settings = configuration.settings.extensions.get("com.toposync.home_assistant", {})
    if not parsed.server_id or parsed.server_id not in {
        server.id for server in resolve_home_assistant_servers(settings)
    }:
        raise ValueError("Select a configured Home Assistant server")
    if _parse_notify_service(parsed.notify_service) is None:
        raise ValueError("Select an explicit notification destination")
    return {"operator": "home_assistant.notify", "config": parsed.model_dump(mode="json")}
