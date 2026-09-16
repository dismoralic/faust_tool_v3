from userbot.core.ftg_compat import (
    everyone,
    group_admin,
    group_admin_add_admins,
    group_admin_ban_users,
    group_admin_change_info,
    group_admin_delete_messages,
    group_admin_pin_messages,
    group_member,
    group_owner,
    owner,
    pm,
    sudo,
    support,
    unrestricted,
)

OWNER = "owner"
SUDO = "sudo"
SUPPORT = "support"
GROUP_OWNER = "group_owner"
GROUP_ADMIN = "group_admin"
GROUP_ADMIN_ADD_ADMINS = "group_admin_add_admins"
GROUP_ADMIN_CHANGE_INFO = "group_admin_change_info"
GROUP_ADMIN_BAN_USERS = "group_admin_ban_users"
GROUP_ADMIN_DELETE_MESSAGES = "group_admin_delete_messages"
GROUP_ADMIN_PIN_MESSAGES = "group_admin_pin_messages"
GROUP_MEMBER = "group_member"
PM = "pm"
EVERYONE = "everyone"

__all__ = [
    "owner", "sudo", "support", "group_owner", "group_admin",
    "group_admin_add_admins", "group_admin_change_info", "group_admin_ban_users",
    "group_admin_delete_messages", "group_admin_pin_messages", "group_member",
    "pm", "everyone", "unrestricted",
]
