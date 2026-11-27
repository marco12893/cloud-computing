"""Proses terpisah yang menjalankan SATU bot peserta.

Dijalankan oleh worker.py sebagai user Linux tanpa hak akses (bot_a / bot_b),
dengan environment kosong dan batas CPU/memori. Komunikasi dengan worker
hanya lewat JSON satu baris per giliran, bukan pickle, sehingga bot tidak
bisa mengirim objek berbahaya ke proses worker.

Protokol:
  stdin  <- satu baris JSON per giliran: [turn, my_lives, my_energy,
            opponent_lives, opponent_energy, my_history, opponent_history]
            atau "null" untuk berhenti.
  stdout -> satu baris JSON berisi aksi bot, atau "null" kalau bot error.
"""

import importlib.util
import json
import os
import sys


def main():
    bot_path = sys.argv[1]

    # Simpan saluran protokol, lalu arahkan print() milik bot ke stderr
    # supaya output bot tidak merusak protokol JSON.
    protocol_out = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    try:
        spec = importlib.util.spec_from_file_location("participant_bot", bot_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        choose_action = module.choose_action
    except BaseException:
        # Bot gagal dimuat: keluar. Worker menganggap bot diam setiap giliran.
        sys.exit(1)

    for line in sys.stdin:
        arguments = json.loads(line)
        if arguments is None:
            break
        try:
            payload = json.dumps(choose_action(*arguments))
        except BaseException:
            payload = "null"
        protocol_out.write(payload + "\n")


if __name__ == "__main__":
    main()
