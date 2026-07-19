"""赛车与谁是卧底的内存状态机。插件重载后状态会安全丢弃。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field


WORD_PAIRS: tuple[tuple[str, str], ...] = (
    ("可乐", "雪碧"),
    ("手机", "平板"),
    ("饺子", "包子"),
    ("自行车", "电动车"),
    ("奶茶", "咖啡"),
    ("火锅", "烧烤"),
    ("西瓜", "哈密瓜"),
    ("猫", "狗"),
    ("高铁", "飞机"),
    ("铅笔", "钢笔"),
)


@dataclass
class RaceRound:
    group_id: str
    choices: dict[str, int] = field(default_factory=dict)


@dataclass
class UndercoverGame:
    group_id: str
    host_id: str
    players: dict[str, str] = field(default_factory=dict)
    phase: str = "lobby"  # lobby / discussion / vote / finished
    civilian_word: str = ""
    undercover_word: str = ""
    undercover_id: str = ""
    alive: set[str] = field(default_factory=set)
    votes: dict[str, str] = field(default_factory=dict)


class GameManager:
    def __init__(self) -> None:
        self.races: dict[str, RaceRound] = {}
        self.undercover: dict[str, UndercoverGame] = {}

    def start_race(self, group_id: str) -> RaceRound | None:
        if group_id in self.races:
            return None
        race = RaceRound(group_id=group_id)
        self.races[group_id] = race
        return race

    def choose_race_car(self, group_id: str, user_id: str, car: int) -> bool:
        race = self.races.get(group_id)
        if not race or car not in range(1, 7):
            return False
        race.choices[user_id] = car
        return True

    def finish_race(self, group_id: str, winner: int | None = None) -> tuple[int, list[str]] | None:
        race = self.races.pop(group_id, None)
        if not race:
            return None
        winner = winner if winner in range(1, 7) else secrets.randbelow(6) + 1
        winners = [user_id for user_id, car in race.choices.items() if car == winner]
        return winner, winners

    def create_undercover_lobby(self, group_id: str, host_id: str, host_name: str) -> UndercoverGame | None:
        if group_id in self.undercover:
            return None
        game = UndercoverGame(group_id=group_id, host_id=host_id, players={host_id: host_name})
        self.undercover[group_id] = game
        return game

    def join_undercover(self, group_id: str, user_id: str, user_name: str) -> tuple[bool, str]:
        game = self.undercover.get(group_id)
        if not game:
            return False, "本群还没有卧底大厅，先发送“谁是卧底”。"
        if game.phase != "lobby":
            return False, "游戏已经开始，不能再加入。"
        if user_id in game.players:
            return False, "你已经在大厅中。"
        if len(game.players) >= 10:
            return False, "本局人数已满（最多 10 人）。"
        game.players[user_id] = user_name
        return True, f"{user_name} 加入成功，当前 {len(game.players)} 人。"

    def prepare_undercover_start(self, group_id: str, host_id: str) -> tuple[UndercoverGame | None, str]:
        game = self.undercover.get(group_id)
        if not game:
            return None, "本群没有正在等待的卧底游戏。"
        if game.host_id != host_id:
            return None, "只有本局发起人可以开始游戏。"
        if game.phase != "lobby":
            return None, "游戏已经开始。"
        if len(game.players) < 4:
            return None, "至少需要 4 人才能开始。"
        pair = secrets.choice(WORD_PAIRS)
        undercover_id = secrets.choice(list(game.players))
        game.civilian_word, game.undercover_word = pair
        game.undercover_id = undercover_id
        return game, ""

    def activate_undercover(self, group_id: str) -> bool:
        game = self.undercover.get(group_id)
        if not game or not game.undercover_id:
            return False
        game.phase = "discussion"
        game.alive = set(game.players)
        game.votes.clear()
        return True

    def cancel_undercover(self, group_id: str) -> bool:
        return self.undercover.pop(group_id, None) is not None

    def start_vote(self, group_id: str, requester_id: str) -> tuple[bool, str]:
        game = self.undercover.get(group_id)
        if not game or game.phase == "finished":
            return False, "当前没有进行中的卧底游戏。"
        if game.host_id != requester_id:
            return False, "只有本局发起人可以开始投票。"
        if game.phase != "discussion":
            return False, "当前不在描述环节。"
        game.phase = "vote"
        game.votes.clear()
        return True, "投票已开始。存活玩家发送“投票 @成员”或“投票 QQ号”。"

    def cast_vote(self, group_id: str, voter_id: str, target_id: str) -> tuple[bool, str, bool]:
        game = self.undercover.get(group_id)
        if not game or game.phase != "vote":
            return False, "现在不是投票时间。", False
        if voter_id not in game.alive:
            return False, "你已出局，不能投票。", False
        if target_id not in game.alive:
            return False, "目标不在存活玩家中。", False
        if voter_id in game.votes:
            return False, "你已经投过票了。", False
        game.votes[voter_id] = target_id
        return True, "投票成功。", len(game.votes) == len(game.alive)

    def finish_vote(self, group_id: str) -> str | None:
        game = self.undercover.get(group_id)
        if not game or game.phase != "vote":
            return None
        if not game.votes:
            game.phase = "discussion"
            return "本轮无人投票，继续描述。"
        counts: dict[str, int] = {}
        for target in game.votes.values():
            counts[target] = counts.get(target, 0) + 1
        most = max(counts.values())
        candidates = [user_id for user_id, count in counts.items() if count == most]
        if len(candidates) != 1:
            game.phase = "discussion"
            game.votes.clear()
            return "本轮投票平局，没有人出局，继续描述。"
        eliminated = candidates[0]
        name = game.players.get(eliminated, eliminated)
        game.alive.discard(eliminated)
        if eliminated == game.undercover_id:
            game.phase = "finished"
            self.undercover.pop(group_id, None)
            return f"{name} 出局，他就是卧底！平民获胜。"
        civilians_alive = len(game.alive - {game.undercover_id})
        if civilians_alive <= 1:
            game.phase = "finished"
            self.undercover.pop(group_id, None)
            return f"{name} 出局，他是平民。卧底“{game.players.get(game.undercover_id)}”获胜！"
        game.phase = "discussion"
        game.votes.clear()
        return f"{name} 出局，他是平民。请继续描述，发起人可再次发送“开始投票”。"
