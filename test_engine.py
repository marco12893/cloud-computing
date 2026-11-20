import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from engine import attack, create_round_robin_schedule, defend, play_match, run_tournament


def make_bot(name, actions):
    def choose_action(turn, my_lives, my_energy, opponent_lives, opponent_energy, my_action_history, opponent_action_history):
        return actions[turn - 1] if turn <= len(actions) else defend("A")
    return {"name": name, "choose_action": choose_action}


class EngineTests(unittest.TestCase):
    def test_first_turn_and_regeneration(self):
        result = play_match(make_bot("A", [attack(1)]), make_bot("B", [defend("B")]), 1)
        self.assertEqual((result["lives_a"], result["lives_b"]), (3, 2))
        self.assertEqual(result["history"][0]["after_energy"], (1, 2))

    def test_matching_defence_blocks_attack(self):
        result = play_match(make_bot("A", [attack(1)]), make_bot("B", [defend("A")]), 1)
        self.assertEqual((result["lives_a"], result["lives_b"]), (3, 3))

    def test_larger_attack_wins_clash(self):
        result = play_match(make_bot("A", [defend("A"), attack(1)]), make_bot("B", [defend("A"), attack(2)]), 2)
        self.assertEqual(result["history"][1]["after_lives"], (2, 3))

    def test_turn_limit_is_draw(self):
        a, b = make_bot("A", [attack(1)]), make_bot("B", [defend("B")])
        self.assertIsNone(play_match(a, b, 1)["winner"])
        table, _ = run_tournament([a, b])
        self.assertEqual([row["points"] for row in table], [1, 1])

    def test_schedule_has_three_rounds_for_four_bots(self):
        bots = [make_bot(str(i), []) for i in range(4)]
        schedule = create_round_robin_schedule(bots)
        self.assertEqual([len(round_pairs) for round_pairs in schedule], [2, 2, 2])

    def test_bot_receives_previous_actions_in_history(self):
        seen_opponent_history = []

        def observer(turn, my_lives, my_energy, opponent_lives, opponent_energy, my_history, opponent_history):
            seen_opponent_history.append(opponent_history)
            return defend("A")

        opponent = make_bot("Lawan", [attack(1), attack(1)])
        play_match({"name": "Pengamat", "choose_action": observer}, opponent, 2)
        self.assertEqual(seen_opponent_history[0], [])
        self.assertEqual(seen_opponent_history[1], [attack(1)])

    def test_bot_cannot_mutate_history_seen_by_opponent(self):
        seen_opponent_history = []

        def mutator(turn, my_lives, my_energy, opponent_lives, opponent_energy, my_history, opponent_history):
            if my_history:
                my_history[-1]["energy"] = 10
            return attack(1)

        def observer(turn, my_lives, my_energy, opponent_lives, opponent_energy, my_history, opponent_history):
            seen_opponent_history.append(opponent_history)
            return defend("A")

        play_match({"name": "Pengubah", "choose_action": mutator}, {"name": "Pengamat", "choose_action": observer}, 2)
        self.assertEqual(seen_opponent_history[1], [attack(1)])

    def test_tournament_saves_one_json_replay_per_match(self):
        with TemporaryDirectory() as directory:
            run_tournament([make_bot("A", [attack(1)]), make_bot("B", [defend("A")])], replay_directory=directory)
            replay_files = list(Path(directory).glob("*.json"))
            self.assertEqual(len(replay_files), 1)
            with replay_files[0].open(encoding="utf-8") as replay_file:
                replay = json.load(replay_file)
            self.assertEqual(replay["format"], "bot-battle-replay-v1")
            self.assertEqual(replay["result"]["bot_a"], "A")


if __name__ == "__main__":
    unittest.main()
