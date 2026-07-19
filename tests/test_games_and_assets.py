from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from games import GameManager


ASSET_HASHES = {
    "group_admin.jpg": "4aeac2b1f51e97b404769330ea372e75da1e396838ff0f3990b89e05431dda78",
    "menu.jpg": "933a736a0be6a0fd0556e16923e8d0ee2ff7a670a669c5b6c4117f32d37548f7",
    "playground.jpg": "5606b157a075f13b2b17ba194e1d4ecd855fca7a627e52e82ae15a6cd797ff62",
    "rules.jpg": "5c823badb21a74bc0186dfcea83bf0629f05fe94967ebd3f40dd1dd0fdbf1308",
    "security.jpg": "6dad558f02c795a8121ac27365622646e6b5430148dd706edac51a208ebb486c",
    "statistics.jpg": "0734e6ecbabe861c074487163f69b63a36f532c43b82aa6e43c4049692863972",
}


class GameAndAssetTests(unittest.TestCase):
    def test_assets_are_original_bytes(self) -> None:
        asset_dir = Path(__file__).resolve().parents[1] / "assets"
        for filename, expected_hash in ASSET_HASHES.items():
            self.assertEqual(hashlib.sha256((asset_dir / filename).read_bytes()).hexdigest(), expected_hash)

    def test_race_and_undercover_flow(self) -> None:
        games = GameManager()
        self.assertIsNotNone(games.start_race("g"))
        self.assertTrue(games.choose_race_car("g", "u1", 3))
        self.assertEqual(games.finish_race("g", winner=3), (3, ["u1"]))

        self.assertIsNotNone(games.create_undercover_lobby("g", "u1", "一号"))
        for user_id in ("u2", "u3", "u4"):
            self.assertTrue(games.join_undercover("g", user_id, user_id)[0])
        game, error = games.prepare_undercover_start("g", "u1")
        self.assertEqual(error, "")
        self.assertIsNotNone(game)
        self.assertTrue(games.activate_undercover("g"))
        self.assertTrue(games.start_vote("g", "u1")[0])
        assert game is not None
        for voter in list(game.alive):
            games.cast_vote("g", voter, game.undercover_id)
        self.assertIn("平民获胜", games.finish_vote("g") or "")


if __name__ == "__main__":
    unittest.main()
