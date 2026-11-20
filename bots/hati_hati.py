BOT_NAME = "Hati-hati"


def choose_action(turn, my_lives, my_energy, opponent_lives, opponent_energy, my_action_history, opponent_action_history):
    # Kumpulkan energi sambil mencoba menebak tipe attack lawan.
    if my_energy >= 5:
        return {"type": "attack", "energy": 5}
    defence = ("A", "B", "C")[(turn - 1) % 3]
    return {"type": "defence", "kind": defence}
