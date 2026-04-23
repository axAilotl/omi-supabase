import logging

logger = logging.getLogger(__name__)


def _log_skip(name: str, *args, **kwargs) -> None:
    logger.info("Skipping %s in Supabase mode; remote push notifications are disabled", name)


def send_notification(user_id: str, title: str, body: str, data: dict = None, tokens: list = None):
    _log_skip("send_notification", user_id, title, body, data, tokens)


async def send_subscription_paid_personalized_notification(user_id: str, data: dict = None):
    _log_skip("send_subscription_paid_personalized_notification", user_id, data)


async def send_credit_limit_notification(user_id: str):
    _log_skip("send_credit_limit_notification", user_id)


async def send_silent_user_notification(user_id: str):
    _log_skip("send_silent_user_notification", user_id)


async def send_training_data_submitted_notification(user_id: str):
    _log_skip("send_training_data_submitted_notification", user_id)


def send_bulk_notification(user_ids: list[str], title: str, body: str, data: dict = None):
    _log_skip("send_bulk_notification", user_ids, title, body, data)


def send_app_review_reply_notification(user_id: str, app_name: str, reply_text: str):
    _log_skip("send_app_review_reply_notification", user_id, app_name, reply_text)


def send_new_app_review_notification(user_id: str, app_name: str):
    _log_skip("send_new_app_review_notification", user_id, app_name)


def send_action_item_data_message(user_id: str, action_item_id: str, data: dict):
    _log_skip("send_action_item_data_message", user_id, action_item_id, data)


def send_apple_reminders_sync_push(user_id: str):
    _log_skip("send_apple_reminders_sync_push", user_id)


def send_merge_completed_message(user_id: str, conversation_id: str):
    _log_skip("send_merge_completed_message", user_id, conversation_id)


def send_important_conversation_message(user_id: str, conversation_id: str):
    _log_skip("send_important_conversation_message", user_id, conversation_id)


def send_action_item_update_message(user_id: str, action_item_id: str, data: dict = None):
    _log_skip("send_action_item_update_message", user_id, action_item_id, data)


def send_action_item_deletion_message(user_id: str, action_item_id: str):
    _log_skip("send_action_item_deletion_message", user_id, action_item_id)


def send_action_item_created_notification(user_id: str, action_item_id: str, title: str):
    _log_skip("send_action_item_created_notification", user_id, action_item_id, title)


def send_action_item_completed_notification(user_id: str, action_item_id: str, title: str):
    _log_skip("send_action_item_completed_notification", user_id, action_item_id, title)
