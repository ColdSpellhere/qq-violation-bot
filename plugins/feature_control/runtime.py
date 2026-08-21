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
    ),
)
