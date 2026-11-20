BOT_NAME = "Peniru"


def choose_action(turn, my_lives, my_energy, opponent_lives, opponent_energy, my_action_history, opponent_action_history):
    # Pertahanan dipilih berdasarkan serangan lawan pada turn sebelumnya.
    if opponent_action_history:
        last_action = opponent_action_history[-1]
        if last_action and last_action["type"] == "attack":
            power = last_action["energy"]
            if power in (1, 3):
                return {"type": "defence", "kind": "A"}
            if power in (2, 4):
                return {"type": "defence", "kind": "B"}
            if power == 5:
                return {"type": "defence", "kind": "C"}
    return {"type": "charge"}
