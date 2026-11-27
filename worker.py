"""Worker Bot Battle Arena untuk AWS Batch / Fargate.

Menjalankan SATU pertandingan antara dua bot peserta:
  1. Mengunduh file bot A dan bot B dari S3 (atau dari folder lokal untuk tes).
  2. Menjalankan engine.play_match dengan setiap bot di proses terpisah yang
     dibatasi (user tanpa hak akses, environment kosong, batas CPU/memori).
  3. Menyimpan replay.json lengkap dan summary.json ke S3.
  4. Mencetak ringkasan ke log (CloudWatch) dengan awalan MATCH_SUMMARY.

Input dikirim lewat environment variable, sehingga Lambda cukup memanggil
batch.submit_job(... containerOverrides={"environment": [...]}) tanpa
mengubah Docker image:

  MATCH_ID        wajib  ID unik pertandingan, misalnya "t1-m0005"
  BOT_A_KEY       wajib  object key bot A, misalnya "bots/user1/v3.py"
  BOT_B_KEY       wajib  object key bot B
  BOT_BUCKET      wajib di mode s3  bucket tempat file bot
  BOT_A_NAME      opsional  nama tampilan bot A (default: BOT_NAME di file)
  BOT_B_NAME      opsional  nama tampilan bot B
  OUTPUT_BUCKET   opsional  bucket hasil (default: BOT_BUCKET)
  OUTPUT_PREFIX   opsional  folder hasil (default: "matches/<MATCH_ID>")
  MAX_TURNS       opsional  default 40
  STORAGE_MODE    opsional  "s3" (default) atau "local" untuk tes tanpa AWS

Mode evaluasi (bot peserta melawan bot referensi yang sudah ada di image):
  EVALUATION_ID   wajib  ID evaluasi dari backend
  BOT_KEY         wajib  object key bot peserta di BOT_BUCKET
  OPPONENT_ID     wajib  bot referensi: agresif, hati_hati, penabung, peniru
  BOT_BUCKET      wajib di mode s3
  MATCH_ID        opsional  default sama dengan EVALUATION_ID
  RESULT_PREFIX   opsional  folder hasil (default: "evaluations/<EVALUATION_ID>")
  OUTPUT_BUCKET   opsional  default BOT_BUCKET
  BOT_NAME        opsional  nama tampilan bot peserta
  Hasil: <RESULT_PREFIX>/report.json dan <RESULT_PREFIX>/replay.json

Exit code:
  0  pertandingan selesai dan hasil tersimpan (termasuk kalau bot error,
     karena itu bagian dari hasil pertandingan)
  2  input tidak lengkap
  3  gagal mengunduh bot (masalah infrastruktur, boleh di-retry oleh Batch)
  4  gagal menyimpan hasil (masalah infrastruktur, boleh di-retry oleh Batch)
"""

import ast
import json
import os
import pwd
import resource
import select
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty

import engine

APP_DIR = Path(__file__).resolve().parent
BOT_RUNNER = APP_DIR / "bot_runner.py"

MAX_BOT_FILE_BYTES = 200 * 1024        # file bot maksimal 200 KB
MAX_REPLY_BYTES = 64 * 1024            # jawaban bot per giliran maksimal 64 KB
BOT_MEMORY_MB = int(os.environ.get("BOT_MEMORY_MB", "256"))
BOT_CPU_SECONDS = int(os.environ.get("BOT_CPU_SECONDS", "30"))
SANDBOX_USERS = {"A": "bot_a", "B": "bot_b"}

# Variabel yang bisa dipakai untuk mendapatkan kredensial atau metadata AWS.
SENSITIVE_ENV_PREFIXES = ("AWS_", "ECS_", "BOTO")


class InputError(Exception):
    pass


class DownloadError(Exception):
    pass


class UploadError(Exception):
    pass


# --------------------------------------------------------------------------
# Penyimpanan: S3 untuk AWS, folder lokal untuk tes di laptop
# --------------------------------------------------------------------------

class S3Storage:
    def __init__(self):
        import boto3  # diimpor di sini supaya mode lokal tidak butuh boto3
        self.client = boto3.client("s3")

    def read_bot(self, bucket, key):
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
            data = response["Body"].read(MAX_BOT_FILE_BYTES + 1)
        except Exception as error:
            raise DownloadError(f"Gagal mengunduh s3://{bucket}/{key}: {error}") from error
        if len(data) > MAX_BOT_FILE_BYTES:
            raise InputError(f"File bot s3://{bucket}/{key} lebih dari {MAX_BOT_FILE_BYTES} byte")
        return data

    def write_json(self, bucket, key, payload):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            self.client.put_object(Bucket=bucket, Key=key, Body=body,
                                   ContentType="application/json")
        except Exception as error:
            raise UploadError(f"Gagal menyimpan s3://{bucket}/{key}: {error}") from error
        return f"s3://{bucket}/{key}"


class LocalStorage:
    """Untuk tes tanpa AWS: bot dibaca dari LOCAL_INPUT_DIR, hasil ke LOCAL_OUTPUT_DIR."""

    def __init__(self):
        self.input_dir = Path(os.environ.get("LOCAL_INPUT_DIR", APP_DIR / "bots"))
        self.output_dir = Path(os.environ.get("LOCAL_OUTPUT_DIR", APP_DIR / "output"))

    def read_bot(self, bucket, key):
        path = self.input_dir / key
        try:
            data = path.read_bytes()
        except OSError as error:
            raise DownloadError(f"Gagal membaca {path}: {error}") from error
        if len(data) > MAX_BOT_FILE_BYTES:
            raise InputError(f"File bot {path} lebih dari {MAX_BOT_FILE_BYTES} byte")
        return data

    def write_json(self, bucket, key, payload):
        path = self.output_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            raise UploadError(f"Gagal menyimpan {path}: {error}") from error
        return str(path)


# --------------------------------------------------------------------------
# Menjalankan bot di proses terisolasi
# --------------------------------------------------------------------------

def _sandbox_available():
    if os.name != "posix" or os.geteuid() != 0:
        return False
    try:
        for name in SANDBOX_USERS.values():
            pwd.getpwnam(name)
    except KeyError:
        return False
    return True


def _limit_resources():
    """Dijalankan di proses anak tepat sebelum Python bot dimulai."""
    memory = BOT_MEMORY_MB * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (BOT_CPU_SECONDS, BOT_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class _ProcessHandle:
    """Meniru antarmuka multiprocessing.Process yang dipakai engine.py."""

    def __init__(self, popen):
        self.popen = popen

    def is_alive(self):
        return self.popen.poll() is None

    def join(self, timeout=None):
        try:
            self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass

    def terminate(self):
        if self.popen.poll() is None:
            try:
                os.killpg(self.popen.pid, 9)
            except (ProcessLookupError, PermissionError):
                self.popen.kill()


class _RequestPipe:
    """Mengirim argumen giliran ke bot sebagai JSON (pengganti request_queue)."""

    def __init__(self, popen):
        self.popen = popen

    def put(self, arguments):
        try:
            self.popen.stdin.write((json.dumps(arguments) + "\n").encode("utf-8"))
            self.popen.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass  # bot sudah mati; engine akan menganggapnya tidak memberi aksi

    def close(self):
        try:
            self.popen.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        # Hentikan juga proses lain yang mungkin dibuat bot (misalnya lewat fork).
        try:
            os.killpg(self.popen.pid, 9)
        except (ProcessLookupError, PermissionError, OSError):
            pass


class _ResponsePipe:
    """Membaca jawaban bot sebagai JSON dengan batas waktu (pengganti result_queue)."""

    def __init__(self, popen):
        self.popen = popen
        self.buffer = b""

    def get(self, timeout):
        deadline = time.monotonic() + timeout
        fd = self.popen.stdout.fileno()
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Empty
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise Empty
            chunk = os.read(fd, 65536)
            if not chunk:  # bot keluar
                raise Empty
            self.buffer += chunk
            if len(self.buffer) > MAX_REPLY_BYTES:
                self.buffer = b""
                return None
        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            return json.loads(line)
        except ValueError:
            return None

    def close(self):
        try:
            self.popen.stdout.close()
        except OSError:
            pass


def start_sandboxed_runner(bot):
    """Pengganti engine.start_bot_runner: satu proses terisolasi per bot per pertandingan."""
    if "path" not in bot:
        return None
    options = {}
    if _sandbox_available():
        user = pwd.getpwnam(SANDBOX_USERS[bot["side"]])
        options = {"user": user.pw_uid, "group": user.pw_gid, "extra_groups": []}
    popen = subprocess.Popen(
        [sys.executable, "-I", str(BOT_RUNNER), bot["path"]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,  # print() dari bot tidak masuk ke log
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",
             "PYTHONDONTWRITEBYTECODE": "1"},  # environment kosong: tanpa kredensial AWS
        cwd="/tmp" if os.name == "posix" else None,
        preexec_fn=_limit_resources if os.name == "posix" else None,
        start_new_session=True,  # grup proses sendiri, bisa dihentikan bersama anaknya
        **options,
    )
    return {"process": _ProcessHandle(popen),
            "request_queue": _RequestPipe(popen),
            "result_queue": _ResponsePipe(popen)}


# --------------------------------------------------------------------------
# Persiapan input dan penyusunan hasil
# --------------------------------------------------------------------------

def read_config():
    env = os.environ
    mode = env.get("STORAGE_MODE", "s3").lower()
    bucket_required = ["BOT_BUCKET"] if mode == "s3" else []

    if env.get("EVALUATION_ID") or env.get("BOT_KEY"):
        required = ["EVALUATION_ID", "BOT_KEY", "OPPONENT_ID"] + bucket_required
        missing = [name for name in required if not env.get(name)]
        if missing:
            raise InputError("Environment variable wajib belum diisi: " + ", ".join(missing))
        evaluation_id = env["EVALUATION_ID"]
        bot_bucket = env.get("BOT_BUCKET", "")
        return {
            "mode": mode,
            "kind": "evaluation",
            "evaluation_id": evaluation_id,
            "opponent_id": env["OPPONENT_ID"],
            "match_id": env.get("MATCH_ID") or evaluation_id,
            "bot_bucket": bot_bucket,
            "bots": {
                "A": {"key": env["BOT_KEY"], "name": env.get("BOT_NAME")},
                "B": {"builtin": env["OPPONENT_ID"], "name": None},
            },
            "output_bucket": env.get("OUTPUT_BUCKET") or bot_bucket,
            "output_prefix": env.get("RESULT_PREFIX", f"evaluations/{evaluation_id}").strip("/"),
            "result_file": "report.json",
            "max_turns": int(env.get("MAX_TURNS", engine.MAX_TURNS)),
        }

    required = ["MATCH_ID", "BOT_A_KEY", "BOT_B_KEY"] + bucket_required
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise InputError("Environment variable wajib belum diisi: " + ", ".join(missing))
    match_id = env["MATCH_ID"]
    bot_bucket = env.get("BOT_BUCKET", "")
    return {
        "mode": mode,
        "kind": "match",
        "match_id": match_id,
        "bot_bucket": bot_bucket,
        "bots": {
            "A": {"key": env["BOT_A_KEY"], "name": env.get("BOT_A_NAME")},
            "B": {"key": env["BOT_B_KEY"], "name": env.get("BOT_B_NAME")},
        },
        "output_bucket": env.get("OUTPUT_BUCKET") or bot_bucket,
        "output_prefix": env.get("OUTPUT_PREFIX", f"matches/{match_id}").strip("/"),
        "result_file": "summary.json",
        "max_turns": int(env.get("MAX_TURNS", engine.MAX_TURNS)),
    }


def read_builtin_bot(bot_id):
    """Bot referensi yang ikut di dalam image (folder bots/)."""
    if not bot_id.replace("_", "").isalnum():
        raise InputError(f"OPPONENT_ID tidak valid: {bot_id}")
    path = APP_DIR / "bots" / f"{bot_id.lower()}.py"
    if not path.is_file():
        available = sorted(p.stem for p in (APP_DIR / "bots").glob("*.py") if not p.name.startswith("_"))
        raise InputError(f"Bot referensi '{bot_id}' tidak ada. Pilihan: {', '.join(available)}")
    return path.read_bytes(), f"builtin:{bot_id.lower()}"


def bot_name_from_source(source, fallback):
    """Membaca BOT_NAME tanpa menjalankan kode peserta."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return fallback
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "BOT_NAME" for target in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value[:40] or fallback
    return fallback


def prepare_bots(config, storage, workdir):
    """Unduh kedua bot ke workdir. Setiap file hanya bisa dibaca oleh user bot pemiliknya."""
    sandbox = _sandbox_available()
    os.chmod(workdir, 0o711)  # bisa dilewati, tapi isinya tidak bisa didaftar
    bots = {}
    for side in ("A", "B"):
        info = config["bots"][side]
        if "builtin" in info:
            data, key = read_builtin_bot(info["builtin"])
        else:
            data, key = storage.read_bot(config["bot_bucket"], info["key"]), info["key"]
        path = workdir / f"bot_{side.lower()}.py"
        path.write_bytes(data)
        if sandbox:
            user = pwd.getpwnam(SANDBOX_USERS[side])
            os.chown(path, user.pw_uid, user.pw_gid)
            os.chmod(path, 0o400)  # bot lawan tidak bisa membaca kode ini
        source = data.decode("utf-8", errors="replace")
        name = info["name"] or bot_name_from_source(source, Path(key).stem)
        bots[side] = {"name": name, "path": str(path), "side": side, "key": key}
    if bots["A"]["name"] == bots["B"]["name"]:  # engine membedakan pemenang dari nama
        bots["A"]["name"] += " (A)"
        bots["B"]["name"] += " (B)"
    return bots


def scrub_sensitive_env():
    for name in list(os.environ):
        if name.startswith(SENSITIVE_ENV_PREFIXES):
            os.environ.pop(name, None)


def build_replay(config, bots, result):
    def side_view(values):
        return {"A": values[0], "B": values[1]}

    turns = [{
        "turn": log["turn"],
        "before": {"lives": side_view(log["before_lives"]), "energy": side_view(log["before_energy"])},
        "actions": side_view(log["actions"]),
        "notes": log["notes"],
        "after": {"lives": side_view(log["after_lives"]), "energy": side_view(log["after_energy"])},
    } for log in result["history"]]
    return {
        "matchId": config["match_id"],
        "maxTurns": config["max_turns"],
        "bots": {side: {"name": bots[side]["name"], "key": bots[side]["key"]} for side in ("A", "B")},
        "turns": turns,
    }


def build_summary(config, bots, result, replay_location):
    if result["winner"] == bots["A"]["name"]:
        winner, outcome, points = "A", "A_WIN", (3, 0)
    elif result["winner"] == bots["B"]["name"]:
        winner, outcome, points = "B", "B_WIN", (0, 3)
    else:
        winner, outcome, points = None, "DRAW", (1, 1)
    invalid = [sum(1 for log in result["history"] if log["actions"][i] is None) for i in (0, 1)]
    lives = (result["lives_a"], result["lives_b"])
    return {
        "matchId": config["match_id"],
        "status": "COMPLETED",
        "result": outcome,
        "winner": winner,
        "winnerName": bots[winner]["name"] if winner else None,
        "turns": result["turns"],
        "botA": {"name": bots["A"]["name"], "key": bots["A"]["key"], "livesLeft": lives[0],
                 "lifeDifference": lives[0] - lives[1], "points": points[0], "invalidActions": invalid[0]},
        "botB": {"name": bots["B"]["name"], "key": bots["B"]["key"], "livesLeft": lives[1],
                 "lifeDifference": lives[1] - lives[0], "points": points[1], "invalidActions": invalid[1]},
        "replay": replay_location,
        "finishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def run():
    config = read_config()
    storage = LocalStorage() if config["mode"] == "local" else S3Storage()
    print(f"[worker] match {config['match_id']}: sandbox {'aktif' if _sandbox_available() else 'TIDAK aktif'}")

    workdir = Path(tempfile.mkdtemp(prefix="match-"))
    bots = prepare_bots(config, storage, workdir)
    print(f"[worker] {bots['A']['name']} ({bots['A']['key']}) vs {bots['B']['name']} ({bots['B']['key']})")

    # Klien S3 sudah memegang kredensial di memori. Hapus jejak kredensial dari
    # environment sebelum kode peserta berjalan.
    scrub_sensitive_env()
    engine.start_bot_runner = start_sandboxed_runner
    result = engine.play_match(bots["A"], bots["B"], max_turns=config["max_turns"])

    prefix = config["output_prefix"]
    replay = build_replay(config, bots, result)
    replay_location = storage.write_json(config["output_bucket"], f"{prefix}/replay.json", replay)
    summary = build_summary(config, bots, result, replay_location)
    if config["kind"] == "evaluation":
        summary = {"evaluationId": config["evaluation_id"], "opponentId": config["opponent_id"], **summary}
    summary["resultLocation"] = storage.write_json(
        config["output_bucket"], f"{prefix}/{config['result_file']}", summary)

    engine.print_match(result)
    print("MATCH_SUMMARY " + json.dumps(summary, ensure_ascii=False))


def main():
    try:
        run()
    except InputError as error:
        print(f"[worker] INPUT ERROR: {error}", file=sys.stderr)
        sys.exit(2)
    except DownloadError as error:
        print(f"[worker] DOWNLOAD ERROR: {error}", file=sys.stderr)
        sys.exit(3)
    except UploadError as error:
        print(f"[worker] UPLOAD ERROR: {error}", file=sys.stderr)
        sys.exit(4)


if __name__ == "__main__":
    main()
