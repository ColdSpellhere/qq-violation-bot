from plugins.violation_record.config import CONFIG

from .state import FeatureController, FeatureState


FEATURES = FeatureController(
    CONFIG.runtime_features_path,
    FeatureState(
        business_enabled=CONFIG.business_enabled,
        chat_enabled=CONFIG.chat_enabled,
        group_chat_enabled=CONFIG.group_chat_enabled,
        private_chat_enabled=CONFIG.private_chat_enabled,
        group_chat_allowed_group_ids=CONFIG.group_chat_allowed_group_ids,
        private_chat_allowed_user_ids=CONFIG.private_chat_allowed_user_ids,
        private_memory_enabled=CONFIG.private_memory_enabled,
        relationship_state_enabled=CONFIG.relationship_state_enabled,
        memory_governance_enabled=CONFIG.memory_governance_enabled,
        llm_gateway_enabled=CONFIG.llm_gateway_enabled,
        economy_mode_enabled=CONFIG.economy_mode_enabled,
        prompt_builder_enabled=CONFIG.prompt_builder_enabled,
        web_search_enabled=CONFIG.web_search_enabled,
        llm_gateway_vision_enabled=CONFIG.llm_gateway_vision_enabled,
        llm_gateway_private_memory_enabled=CONFIG.llm_gateway_private_memory_enabled,
        llm_gateway_member_memory_enabled=CONFIG.llm_gateway_member_memory_enabled,
        llm_gateway_chat_enabled=CONFIG.llm_gateway_chat_enabled,
        llm_gateway_business_enabled=CONFIG.llm_gateway_business_enabled,
    ),
    business_capable=CONFIG.business_capable,
    economy_provider_available=CONFIG.economy_provider_available,
    primary_provider_available=bool(CONFIG.ai_api_key.strip()),
)
