"""Discord bot discovery and delivery through the official REST API."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .discord_webhook import DiscordAttachment, DiscordWebhookError, discord_request

_API = "https://discord.com/api/v10"
_REQUIRED = (1 << 10) | (1 << 11) | (1 << 14) | (1 << 15)


def validate_bot_token(value: object) -> str:
    token = str(value or "").strip()
    if token.lower().startswith("bot "):
        token = token[4:].strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{20,256}", token):
        raise ValueError("請輸入有效的 Discord Bot Token。")
    return token


def validate_channel_id(value: object) -> str:
    channel_id = str(value or "").strip()
    if not re.fullmatch(r"[0-9]{5,30}", channel_id):
        raise ValueError("請先連線機器人並選擇播報頻道。")
    return channel_id


def _can_broadcast(base: int, guild_id: str, member: Mapping[str, Any], channel: Mapping[str, Any]) -> bool:
    if base & (1 << 3):  # Administrator bypasses channel overwrites.
        return True
    overwrites = channel.get("permission_overwrites", [])
    roles = {str(role) for role in member.get("roles", [])}
    user_id = str(member.get("user", {}).get("id", ""))
    for kind in ("everyone", "roles", "member"):
        allow = deny = 0
        for overwrite in overwrites:
            target = str(overwrite.get("id", ""))
            role = overwrite.get("type") == 0
            matches = (
                (kind == "everyone" and role and target == guild_id)
                or (kind == "roles" and role and target != guild_id and target in roles)
                or (kind == "member" and not role and target == user_id)
            )
            if matches:
                allow |= int(overwrite.get("allow", 0))
                deny |= int(overwrite.get("deny", 0))
        base = (base & ~deny) | allow
    return base & _REQUIRED == _REQUIRED


def discover_bot_channels(bot_token: object) -> dict[str, Any]:
    token = validate_bot_token(bot_token)

    def get(path: str) -> Any:
        return discord_request(f"{_API}{path}", bot_token=token)

    user = get("/users/@me")
    if user.get("bot") is not True:
        raise ValueError("請使用 Discord 機器人的 Bot Token。")
    guilds: list[dict[str, Any]] = []
    after = ""
    while True:
        page = get(f"/users/@me/guilds?limit=200{after}")
        guilds.extend(page)
        if len(page) < 200:
            break
        after = f"&after={validate_channel_id(page[-1]['id'])}"
    channels: list[dict[str, str]] = []
    warnings: list[str] = []
    for guild in guilds:
        guild_id = validate_channel_id(guild["id"])
        try:
            member = get(f"/guilds/{guild_id}/members/{validate_channel_id(user['id'])}")
            available = get(f"/guilds/{guild_id}/channels")
        except DiscordWebhookError as exc:
            if exc.status != 403:
                raise
            warnings.append(f"無法讀取伺服器「{str(guild.get('name', ''))[:100]}」的頻道，請確認機器人權限。")
            continue
        categories = {str(item["id"]): str(item.get("name", "")) for item in available if item.get("type") == 4}
        for channel in sorted(available, key=lambda item: int(item.get("position", 0))):
            if channel.get("type") not in {0, 5}:
                continue
            if not _can_broadcast(int(guild.get("permissions", 0)), guild_id, member, channel):
                continue
            channels.append({
                "id": validate_channel_id(channel["id"]),
                "name": str(channel.get("name", "")),
                "guild_name": str(guild.get("name", "")),
                "category_name": categories.get(str(channel.get("parent_id")), ""),
            })
    if not channels and not warnings:
        warnings.append("沒有可播報的文字頻道。請先邀請機器人加入伺服器，並授予檢視頻道、傳送訊息、嵌入連結及附加檔案權限。")
    return {
        "bot_name": str(user.get("global_name") or user.get("username") or "Discord Bot"),
        "channels": channels,
        "warnings": warnings,
    }


def resolve_bot_channel(bot_token: object, channel_id: object = "") -> str:
    if str(channel_id or "").strip():
        return validate_channel_id(channel_id)
    result = discover_bot_channels(bot_token)
    if len(result["channels"]) == 1 and not result["warnings"]:
        return result["channels"][0]["id"]
    if not result["channels"]:
        raise ValueError(" ".join(result["warnings"]))
    raise ValueError("請先連線機器人，從清單選擇要播報的頻道。")


def post_discord_bot(
    bot_token: object,
    channel_id: object,
    payload: Mapping[str, Any],
    *,
    files: Sequence[DiscordAttachment] = (),
) -> None:
    token = validate_bot_token(bot_token)
    channel = validate_channel_id(channel_id)
    body = {key: value for key, value in payload.items() if key not in {"username", "avatar_url"}}
    discord_request(f"{_API}/channels/{channel}/messages", body, bot_token=token, files=files)
