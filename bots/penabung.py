BOT_NAME = "Penabung"


def choose_action(turn, my_lives, my_energy, opponent_lives, opponent_energy, my_action_history, opponent_action_history):
    # Charge sampai dapat memakai attack 10, yang tidak bisa ditahan Defence.
    if my_energy >= 10:
        return {"type": "attack", "energy": 10}
    return {"type": "charge"}
