BOT_NAME = "Agresif"


def choose_action(turn, my_lives, my_energy, opponent_lives, opponent_energy, my_action_history, opponent_action_history):
    # Gunakan serangan terbesar yang mampu dibayar.
    for power in (10, 5, 4, 3, 2, 1):
        if my_energy >= power:
            return {"type": "attack", "energy": power}
    return {"type": "charge"}
