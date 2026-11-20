"""Engine Bot Battle tanpa class. Jalankan: python engine.py."""

from copy import deepcopy
from datetime import datetime
import importlib.util
import json
import multiprocessing
from pathlib import Path
from queue import Empty

MAX_TURNS = 40
BOT_TIMEOUT_SECONDS = 5
ATTACK_COSTS = {1, 2, 3, 4, 5, 10}
DEFENCE_BLOCKS = {"A": {1, 3}, "B": {2, 4}, "C": {5}}


def attack(energy):
    return {"type": "attack", "energy": energy}


def defend(kind):
    return {"type": "defence", "kind": str(kind).upper()}


def charge():
    return {"type": "charge"}


def valid_action(action, available_energy):
    if not isinstance(action, dict):
        return False
    if action.get("type") == "attack":
        power = action.get("energy")
        return type(power) is int and power in ATTACK_COSTS and power <= available_energy
    if action.get("type") == "defence":
        return str(action.get("kind", "")).upper() in DEFENCE_BLOCKS
    return action.get("type") == "charge"


def clean_action(action):
    if action["type"] == "attack":
        return attack(action["energy"])
    if action["type"] == "defence":
        return defend(action["kind"])
    return charge()


def action_text(action):
    if action is None:
        return "Tidak valid / diam"
    if action["type"] == "attack":
        return "Attack " + str(action["energy"])
    if action["type"] == "defence":
        return "Defence " + action["kind"]
    return "Charge"


def _bot_worker(bot_path, request_queue, result_queue):
    """Worker proses terpisah agar bot yang loop dapat dihentikan dengan aman."""
    try:
        spec = importlib.util.spec_from_file_location("battle_bot", bot_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except BaseException:
        return
    while True:
        arguments = request_queue.get()
        if arguments is None:
            return
        try:
            result_queue.put(module.choose_action(*arguments))
        except BaseException:
            # Error bot juga setara dengan tidak memberikan aksi.
            result_queue.put(None)


def start_bot_runner(bot):
    """Buat satu proses peserta untuk satu pertandingan, bukan setiap turn."""
    if "path" not in bot:
        return None
    request_queue, result_queue = multiprocessing.Queue(), multiprocessing.Queue()
    process = multiprocessing.Process(target=_bot_worker, args=(bot["path"], request_queue, result_queue))
    process.start()
    return {"process": process, "request_queue": request_queue, "result_queue": result_queue}


def stop_bot_runner(runner):
    if runner is None:
        return
    process = runner["process"]
    if process.is_alive():
        runner["request_queue"].put(None)
        process.join(timeout=0.2)
    if process.is_alive():
        process.terminate()
        process.join()
    runner["request_queue"].close()
    runner["result_queue"].close()


def ask_bot(bot, turn, my_lives, my_energy, opponent_lives, opponent_energy, my_history, opponent_history, runner=None):
    """Memanggil bot dengan parameter eksplisit, tanpa object state.

    Riwayat berupa list aksi turn yang telah selesai. Misalnya, untuk membaca
    attack lawan terakhir: ``opponent_action_history[-1]["energy"]``.
    Aksi pada turn berjalan belum ada di riwayat agar permainan tetap serentak.
    """
    arguments = (turn, my_lives, my_energy, opponent_lives, opponent_energy,
                 deepcopy(my_history), deepcopy(opponent_history))
    # Pada Windows, Process dapat diterminasi ketika peserta membuat infinite
    # loop. Worker dipakai ulang sepanjang satu pertandingan agar cepat.
    if runner is not None:
        process = runner["process"]
        if not process.is_alive():
            return None
        runner["request_queue"].put(arguments)
        try:
            action = runner["result_queue"].get(timeout=BOT_TIMEOUT_SECONDS)
        except Empty:
            action = None
            if process.is_alive():
                process.terminate()
                process.join()
        return action

    # Jalur ini dipakai hanya oleh bot buatan tes yang tidak berasal dari file.
    try:
        return bot["choose_action"](*arguments)
    except Exception:
        return None


def play_match(bot_a, bot_b, max_turns=MAX_TURNS):
    lives, energy = [3, 3], [1, 1]
    action_history, history = [[], []], []
    runners = [start_bot_runner(bot_a), start_bot_runner(bot_b)]

    for turn in range(1, max_turns + 1):
        before_lives, before_energy = tuple(lives), tuple(energy)
        raw = [
            ask_bot(bot_a, turn, lives[0], energy[0], lives[1], energy[1], action_history[0], action_history[1], runners[0]),
            ask_bot(bot_b, turn, lives[1], energy[1], lives[0], energy[0], action_history[1], action_history[0], runners[1]),
        ]
        actions = [clean_action(raw[i]) if valid_action(raw[i], energy[i]) else None for i in range(2)]
        notes = []
        for i, action in enumerate(actions):
            if raw[i] is not None and action is None:
                notes.append((bot_a if i == 0 else bot_b)["name"] + " melakukan aksi tidak valid")
            if action and action["type"] == "attack":
                energy[i] -= action["energy"]

        power = [a["energy"] if a and a["type"] == "attack" else None for a in actions]
        hit = [False, False]
        if power[0] is not None and power[1] is not None:
            if power[0] > power[1]:
                hit[1], notes = True, notes + [bot_a["name"] + " menang clash Attack"]
            elif power[1] > power[0]:
                hit[0], notes = True, notes + [bot_b["name"] + " menang clash Attack"]
            else:
                notes.append("Attack sama kuat: tidak ada damage")
        else:
            for attacker, defender in ((0, 1), (1, 0)):
                if power[attacker] is None:
                    continue
                other = actions[defender]
                blocked = other and other["type"] == "defence" and power[attacker] in DEFENCE_BLOCKS[other["kind"]]
                if blocked:
                    notes.append((bot_a if defender == 0 else bot_b)["name"] + " berhasil menahan Attack " + str(power[attacker]))
                else:
                    hit[defender] = True

        for i in range(2):
            if hit[i]:
                lives[i] -= 1
                notes.append((bot_a if i == 0 else bot_b)["name"] + " kehilangan 1 nyawa")
            energy[i] += 1  # regenerasi otomatis terjadi setelah resolusi
            if actions[i] and actions[i]["type"] == "charge":
                energy[i] += 1
                notes.append((bot_a if i == 0 else bot_b)["name"] + " mendapat +1 energi dari Charge")

        # Simpan aksi setelah kedua bot sudah memilih aksi turn ini.
        action_history[0].append(actions[0])
        action_history[1].append(actions[1])
        history.append({"turn": turn, "before_lives": before_lives, "before_energy": before_energy,
                        "actions": actions, "notes": notes, "after_lives": tuple(lives), "after_energy": tuple(energy)})
        if lives[0] <= 0 or lives[1] <= 0:
            break

    stop_bot_runner(runners[0])
    stop_bot_runner(runners[1])

    winner = bot_a["name"] if lives[1] <= 0 < lives[0] else bot_b["name"] if lives[0] <= 0 < lives[1] else None
    return {"bot_a": bot_a["name"], "bot_b": bot_b["name"], "lives_a": lives[0], "lives_b": lives[1],
            "turns": len(history), "winner": winner, "history": history}


def print_match(result):
    a, b = result["bot_a"], result["bot_b"]
    print("\n" + "=" * 62 + "\nPERTANDINGAN: " + a + " vs " + b)
    for log in result["history"]:
        print(f"\nTurn {log['turn']} - sebelum aksi: {a} (nyawa {log['before_lives'][0]}, energi {log['before_energy'][0]}) | {b} (nyawa {log['before_lives'][1]}, energi {log['before_energy'][1]})")
        print("  Aksi: " + a + " = " + action_text(log["actions"][0]) + " | " + b + " = " + action_text(log["actions"][1]))
        print("  Resolusi: " + "; ".join(log["notes"] or ["Tidak ada damage"]))
        print(f"  Setelah regenerasi: {a} (nyawa {log['after_lives'][0]}, energi {log['after_energy'][0]}) | {b} (nyawa {log['after_lives'][1]}, energi {log['after_energy'][1]})")
    print("\nHasil: " + (result["winner"] + " menang" if result["winner"] else "Seri") + f" - nyawa akhir {result['lives_a']}-{result['lives_b']}")


def create_round_robin_schedule(bots):
    """Buat daftar babak dengan metode rotasi; ganjil mendapat bye."""
    rotation = bots[:]
    if len(rotation) % 2:
        rotation.append(None)
    schedule = []
    for _ in range(len(rotation) - 1):
        schedule.append([(rotation[i], rotation[-1 - i]) for i in range(len(rotation) // 2)
                         if rotation[i] is not None and rotation[-1 - i] is not None])
        rotation = [rotation[0], rotation[-1]] + rotation[1:-1]
    return schedule


def sorted_standings(table):
    return sorted(table.values(), key=lambda row: (-row["points"], -row["life_difference"], row["name"]))


def print_standings(standings, title):
    print("\n" + title)
    print("No  Bot                 Main  Menang  Kalah  Draw  Selisih  Poin")
    for rank, row in enumerate(standings, 1):
        print(f"{rank:<3} {row['name']:<19} {row['played']:<5} {row['wins']:<6} {row['losses']:<6} {row['draws']:<5} {row['life_difference']:<8} {row['points']}")


def create_replay_directory(base_directory="replays"):
    """Buat folder replay baru untuk satu kali eksekusi turnamen."""
    base = Path(base_directory)
    base.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
    directory = base / timestamp
    counter = 1
    while directory.exists():
        directory = base / (timestamp + "_" + str(counter))
        counter += 1
    directory.mkdir()
    return directory


def replay_file_name(match_number, result):
    """Nama file yang aman untuk Windows dan tetap menjelaskan pasangan bot."""
    def safe_name(name):
        return "".join(character if character.isalnum() or character in "-_" else "_" for character in name)

    return f"match_{match_number:02d}_{safe_name(result['bot_a'])}_vs_{safe_name(result['bot_b'])}.json"


def save_replay(result, replay_directory, round_number, match_number):
    """Simpan satu pertandingan sebagai JSON yang dapat diputar ulang."""
    replay = {
        "format": "bot-battle-replay-v1",
        "round": round_number,
        "match_number": match_number,
        "result": result,
    }
    path = Path(replay_directory) / replay_file_name(match_number, result)
    with path.open("w", encoding="utf-8") as replay_file:
        json.dump(replay, replay_file, ensure_ascii=False, indent=2)
    return path


def run_tournament(bots, show_progress=False, replay_directory=None):
    table = {b["name"]: {"name": b["name"], "played": 0, "wins": 0, "losses": 0, "draws": 0, "life_difference": 0, "points": 0} for b in bots}
    if len(table) != len(bots):
        raise ValueError("Setiap BOT_NAME harus unik.")
    matches = []
    for round_number, pairs in enumerate(create_round_robin_schedule(bots), 1):
        if show_progress:
            print("\n" + "#" * 62 + "\nBABAK " + str(round_number))
        for bot_a, bot_b in pairs:
            result = play_match(bot_a, bot_b)
            matches.append(result)
            if replay_directory is not None:
                replay_path = save_replay(result, replay_directory, round_number, len(matches))
                if show_progress:
                    print("Replay disimpan: " + str(replay_path))
            a, b = table[result["bot_a"]], table[result["bot_b"]]
            a["played"] += 1; b["played"] += 1
            a["life_difference"] += result["lives_a"] - result["lives_b"]
            b["life_difference"] += result["lives_b"] - result["lives_a"]
            if result["winner"] == result["bot_a"]:
                a["wins"] += 1; b["losses"] += 1; a["points"] += 3
            elif result["winner"] == result["bot_b"]:
                b["wins"] += 1; a["losses"] += 1; b["points"] += 3
            else:
                a["draws"] += 1; b["draws"] += 1; a["points"] += 1; b["points"] += 1
            if show_progress:
                print_match(result)
        if show_progress:
            print_standings(sorted_standings(table), "KLASEMEN SETELAH BABAK " + str(round_number))
    return sorted_standings(table), matches


def load_bots(directory="bots"):
    bots = []
    for path in sorted(Path(directory).glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location("bot_" + path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not isinstance(getattr(module, "BOT_NAME", None), str) or not callable(getattr(module, "choose_action", None)):
            raise ValueError(path.name + " perlu BOT_NAME dan choose_action(...).")
        bots.append({"name": module.BOT_NAME, "choose_action": module.choose_action, "path": str(path.resolve())})
    return bots


def main():
    bots = load_bots(Path(__file__).parent / "bots")
    replay_directory = create_replay_directory(Path(__file__).parent / "replays")
    print("Replay akan disimpan di: " + str(replay_directory))
    print("JADWAL ROUND-ROBIN")
    for number, pairs in enumerate(create_round_robin_schedule(bots), 1):
        print("Babak " + str(number) + ": " + ", ".join(a["name"] + " vs " + b["name"] for a, b in pairs))
    standings, _ = run_tournament(bots, show_progress=True, replay_directory=replay_directory)
    print_standings(standings, "KLASEMEN AKHIR")


if __name__ == "__main__":
    main()
