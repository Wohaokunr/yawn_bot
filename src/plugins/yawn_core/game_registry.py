"""进程内跨玩法的群组与用户占用登记。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

GameKind = Literal["rpg", "werewolf"]


@dataclass(frozen=True)
class _Lease:
    kind: GameKind
    group_id: int
    host_user_id: int


@dataclass(frozen=True, slots=True)
class GameAccess:
    """当前用户所处玩法的通用访问快照。"""

    kind: GameKind
    group_id: int
    is_room_host: bool
    is_player: bool


_groups: dict[int, _Lease] = {}
_users: dict[int, _Lease] = {}


def active_game_kind(group_id: int) -> GameKind | None:
    """返回本群当前占用的玩法；没有活跃玩法时返回 ``None``。"""

    lease = _groups.get(group_id)
    return lease.kind if lease is not None else None


def resolve_game_access(group_id: int | None, user_id: int) -> GameAccess | None:
    """解析当前玩法、房主与参与者身份，不读取具体玩法内部状态。"""

    lease = _groups.get(group_id) if group_id is not None else _users.get(user_id)
    if lease is None:
        return None
    return GameAccess(
        kind=lease.kind,
        group_id=lease.group_id,
        is_room_host=lease.host_user_id == user_id,
        is_player=_users.get(user_id) == lease,
    )


def group_has_game(kind: GameKind, group_id: int) -> bool:
    """判断本群当前是否由指定玩法占用，供共享命令路由使用。"""

    return active_game_kind(group_id) == kind


def reserve_game(kind: GameKind, group_id: int, host_user_id: int) -> bool:
    """原子登记一局玩法，并占用群组与房主。"""
    if group_id in _groups or host_user_id in _users:
        return False
    lease = _Lease(kind, group_id, host_user_id)
    _groups[group_id] = lease
    _users[host_user_id] = lease
    return True


def reserve_user(kind: GameKind, group_id: int, user_id: int) -> bool:
    """登记报名者；同一用户不能跨玩法或跨群重复入局。"""
    lease = _groups.get(group_id)
    if lease is None or lease.kind != kind or user_id in _users:
        return False
    _users[user_id] = lease
    return True


def release_user(kind: GameKind, group_id: int, user_id: int) -> None:
    """释放仍属于指定玩法与群组的用户占用。"""
    lease = _users.get(user_id)
    if lease is not None and lease.kind == kind and lease.group_id == group_id:
        _users.pop(user_id, None)


def release_game(kind: GameKind, group_id: int) -> None:
    """释放一局玩法的群组和全部用户占用。"""
    lease = _groups.get(group_id)
    if lease is None or lease.kind != kind:
        return
    _groups.pop(group_id, None)
    for user_id, user_lease in list(_users.items()):
        if user_lease == lease:
            _users.pop(user_id, None)


def reset_for_tests() -> None:
    """清空登记；仅供单元测试隔离全局状态。"""
    _groups.clear()
    _users.clear()
