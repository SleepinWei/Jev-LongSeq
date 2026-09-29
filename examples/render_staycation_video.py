"""Make a synchronized comparison film from real task-tab frames and model ledgers."""

from __future__ import annotations

import argparse
import bisect
import json
import math
import subprocess
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 2560, 1440
BG, PANEL, INK, MUTED = "#10171d", "#1b252e", "#eef3ef", "#a4b3bc"
COLORS = ["#baabff", "#65dbc4"]
FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"
FFMPEG = "/private/tmp/jev-demo-video-tools/imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-v7.1"


@lru_cache(maxsize=32)
def font(size):
    return ImageFont.truetype(FONT, size)


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def lines(draw, text, xy, width, size=32, fill=INK, spacing=14, max_lines=20):
    x, y = xy
    f = font(size)
    rows = []
    for paragraph in text.split("\n"):
        current = ""
        for char in paragraph:
            if f.getlength(current + char) > width:
                rows.append(current)
                current = ""
            current += char
        rows.append(current)
    for row in rows[:max_lines]:
        draw.text((x, y), row, font=f, fill=fill)
        y += size + spacing
    return y


class Arm:
    def __init__(self, folder, title, color):
        self.folder, self.title, self.color = folder, title, color
        self.report = json.loads((folder / "report.json").read_text())
        self.start = stamp(self.report["manifest"]["started_at"])
        self.duration = self.report["end_to_end_s"]
        self.frames = jsonl(folder / "frames.jsonl")
        self.frame_times = [f["time"] - self.start for f in self.frames]
        self.calls = self.report["model_calls"]
        self.call_ends = [stamp(c["started_at"]) + c["latency_s"] - self.start for c in self.calls]
        self.cost = sum(c.get("cost_usd") or 0 for c in self.calls)
        self.unknown = sum(c.get("cost_usd") is None for c in self.calls)
        self.events = []
        for e in jsonl(folder / "model-request-starts.jsonl"):
            who = "Jev" if e["kind"] == "jev" else "DeepSeek"
            action = {"dynamic_input": "生成搜索词", "llm_policy": "选择下一步动作",
                      "dynamic_finish": "整理结果并复核", "jev": "选择下一步动作"}.get(e["kind"], "阶段规划")
            self.events.append((stamp(e["started_at"]) - self.start, f"{who} · {action}"))
        for e in jsonl(folder / "trajectory.jsonl"):
            if e["kind"] == "action_started":
                a = e["action"]
                labels = {"fill": "输入", "click": "点击", "scroll": "滚动", "wait": "等待页面更新",
                          "back": "返回", "open_url": "打开页面", "switch_tab": "切换标签页"}
                label = labels.get(a["operation"], a["operation"])
                value = a.get("bound_value") or a.get("description", "")
                self.events.append((stamp(e["time"]) - self.start, f"{label} · {value[:65]}"))
        self.events.sort()
        self.event_times = [e[0] for e in self.events]
        self.cached_index, self.cached_image = -2, None

    def draw(self, canvas, side, elapsed):
        d = ImageDraw.Draw(canvas)
        x, y, width = 40 + side * 1264, 194, 1216
        d.rounded_rectangle((x, y, x + width, 1330), radius=24, fill=PANEL)
        d.text((x + 28, y + 20), self.title, font=font(39), fill=self.color)
        t = min(elapsed, self.duration)
        ended = elapsed >= self.duration
        d.text((x + 855, y + 21), f"{t:06.1f} s", font=font(40), fill=INK)
        index = bisect.bisect_right(self.frame_times, t) - 1
        if index >= 0:
            if index != self.cached_index:
                self.cached_image = Image.open(self.folder / self.frames[index]["resource"]).convert("RGB")
                self.cached_image = self.cached_image.resize((1216, 855), Image.Resampling.LANCZOS)
                self.cached_index = index
            canvas.paste(self.cached_image, (x, 278))
        else:
            d.rectangle((x, 278, x + width, 1133), fill="#202d36")
            d.text((x + 230, 635), "连接浏览器 · 准备页面", font=font(45), fill=MUTED)
        j = bisect.bisect_right(self.event_times, t) - 1
        status = self.events[j][1] if j >= 0 else "连接浏览器"
        if ended:
            status = "已结束 · " + ("已输出总结" if self.report["result"]["status"] == "success" else "运行失败，未生成总结")
        lines(d, status, (x + 26, 1150), 1160, size=27, max_lines=2, fill=self.color)
        active = [c for c, end in zip(self.calls, self.call_ends, strict=True) if end <= t]
        cost = self.cost if ended else sum(c.get("cost_usd") or 0 for c in active)
        count = len(self.calls) if ended else len(active)
        d.text((x + 26, 1246), f"已知 API 等价 cost  ${cost:.5f}", font=font(30), fill=INK)
        d.text((x + 895, 1246), f"{count} 次模型请求", font=font(25), fill=MUTED)
        d.rectangle((x, 1323, x + width * min(t / self.duration, 1), 1330), fill=self.color)


def base(kicker, title):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((64, 44), kicker, font=font(26), fill=COLORS[1])
    d.text((62, 96), title, font=font(46), fill=INK)
    return im


def title_card(failed=False):
    im = base("JEV LONGSEQ  /  LIVE WEB COMPARISON", "同一个任务，两种执行方式")
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((90, 255, 2470, 665), radius=28, fill=PANEL)
    lines(d, "在小红书上查找总结最近\n适合 staycation 的地方", (158, 324), 2200, size=76, spacing=28)
    for i, (title, detail) in enumerate([
        ("纯 DeepSeek", "DS 逐步决策 + DS 阶段指导与总结"),
        ("Jev + DeepSeek", "Jev 选择动作 + DS 阶段指导与总结"),
    ]):
        x = 110 + i * 1250
        d.text((x, 788), title, font=font(58), fill=COLORS[i])
        lines(d, detail, (x, 890), 1130, size=35)
    lines(d, "原句执行 · 不限定城市 · 同一已登录浏览器 · 顺序实测后同步回放", (110, 1110), 2300, size=37)
    lines(d, "相同 DOM / 记忆 / 任务预算；仅替换每步决策模型。", (110, 1190), 2300, size=32, fill=MUTED)
    if failed:
        lines(d, "本次录制：纯 DS 完成；Jev + DS 网络中断。有效成功对照尚未完成。", (110, 1290), 2330, size=32, fill="#ffbd9b")
    return im


def results_card(arms, quality):
    im = base("RESULTS  /  实际测量", "耗时、API 等价费用与结果质量")
    d = ImageDraw.Draw(im)
    for i, a in enumerate(arms):
        x = 70 + i * 1260
        d.rounded_rectangle((x, 240, x + 1160, 1135), radius=30, fill=PANEL)
        d.text((x + 42, 280), a.title, font=font(48), fill=a.color)
        d.text((x + 42, 386), f"{a.duration:.1f} 秒", font=font(92), fill=INK)
        d.text((x + 42, 520), f"${a.cost:.5f}", font=font(86), fill=a.color)
        lines(d, f"{len(a.calls)} 次模型请求 · {a.unknown} 次未返回 usage", (x + 42, 650), 1070, size=33, fill=MUTED)
        lines(d, quality[i]["quality"], (x + 42, 747), 1060, size=36, spacing=22, max_lines=6)
    ratio = arms[1].cost / arms[0].cost if arms[0].cost else 0
    # Descriptive pairwise comparison; no claim of general architectural superiority.
    delta = arms[1].duration - arms[0].duration
    faster = f"本轮 Jev + DS 比纯 DS {'快' if delta < 0 else '慢'} {abs(delta):.1f} 秒"
    cost = f"；已知费用为纯 DS 的 {ratio:.2f}×。"
    comparison = faster + cost if all(a.report["result"]["status"] == "success" for a in arms) else "有效对照未完成：有一侧因连接中断，不能据此给出速度或费用胜负。"
    lines(d, comparison, (78, 1178), 2400, size=36)
    if quality[1].get("comparison_note"):
        lines(d, quality[1]["comparison_note"], (78, 1234), 2400, size=27, fill=MUTED)
    lines(d, "费用按实际返回的 token（含缓存）× 官方 off-peak 单价估算；无 usage 项保持未知，不是账户账单。", (78, 1280), 2400, size=27, fill=MUTED)
    return im


def attempts_card(arms, runs):
    im = base("ALL ATTEMPTS  /  保留失败开销", "包含全部尝试后的总投入")
    d = ImageDraw.Draw(im)
    failures = [json.loads(p.read_text()) for p in sorted(runs.glob("jev-ds*/report.json"))
                if p.parent.resolve() != arms[1].folder.resolve()]
    extra_cost = sum(c.get("cost_usd") or 0 for f in failures for c in f["model_calls"])
    extra_time = sum(f["end_to_end_s"] for f in failures)
    total_time = arms[1].duration + extra_time
    total_cost = arms[1].cost + extra_cost
    lines(d, f"Jev + DS 另有 {len(failures)} 次：网络连接失败，未执行搜索动作。", (100, 295), 2300, size=49)
    lines(d, f"其他尝试合计 {extra_time:.1f} 秒；已知 token 费用 ${extra_cost:.5f}。", (100, 405), 2300, size=43)
    d.rounded_rectangle((90, 585, 2470, 1000), radius=28, fill=PANEL)
    lines(d, f"纯 DS：共 {arms[0].duration:.1f} 秒  /  已知费用 ${arms[0].cost:.5f}\nJev + DS：共 {total_time:.1f} 秒  /  已知费用 ${total_cost:.5f}", (152, 650), 2260, size=61, spacing=65)
    lines(d, "以上为代理运行累计，不含排查与剪辑；缺少 usage 时，总账单无法精确还原。", (105, 1080), 2320, size=33, fill=MUTED)
    lines(d, "本轮受网络、搜索词与网页内容影响，不能直接归因于模型架构。", (105, 1175), 2320, size=35, fill=MUTED)
    return im


def answers_card(arms, quality):
    im = base("ANSWER CHECK  /  效果比较", "它们各自给出了什么？")
    d = ImageDraw.Draw(im)
    for i, a in enumerate(arms):
        x = 70 + i * 1260
        d.rounded_rectangle((x, 240, x + 1160, 1200), radius=30, fill=PANEL)
        d.text((x + 42, 282), a.title, font=font(47), fill=a.color)
        lines(d, quality[i]["answer"], (x + 42, 385), 1065, size=39, spacing=23, max_lines=12)
    lines(d, "本次为开放网页单次样本，非独立 benchmark 成绩；最近性、房价和可订情况仍需来源核验。", (80, 1260), 2390, size=28, fill=MUTED)
    return im


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quality", type=Path, required=True)
    parser.add_argument("--jev-run", default="jev-ds")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    arms = [Arm(args.runs / name, title, color) for name, title, color in zip(
        ["pure-ds", args.jev_run], ["纯 DeepSeek", "Jev + DeepSeek"], COLORS, strict=True)]
    quality = json.loads(args.quality.read_text())
    speed = 2 if max(a.duration for a in arms) <= 240 else 4
    fps = 4
    race_seconds = max(a.duration for a in arms) / speed
    cards = [title_card(any(a.report["result"]["status"] != "success" for a in arms)),
             answers_card(arms, quality), results_card(arms, quality)]
    names = ["title", "answers", "results"]
    if len(list(args.runs.glob("jev-ds*/report.json"))) > 1:
        cards.append(attempts_card(arms, args.runs))
        names.append("all-attempts")
    for name, card in zip(names, cards, strict=True):
        card.save(args.output / f"{name}.png")
    target = args.output / "Staycation-PureDS-vs-JevDS.mp4"
    command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
               "-pixel_format", "rgb24", "-video_size", f"{W}x{H}", "-framerate", str(fps),
               "-i", "-", "-an", "-r", "24", "-c:v", "libx264", "-crf", "20",
               "-preset", "fast", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)

    def emit(im, seconds=1 / fps):
        payload = im.tobytes()
        for _ in range(round(seconds * fps)):
            process.stdin.write(payload)

    emit(cards[0], 7)
    for n in range(math.ceil(race_seconds * fps) + 1):
        elapsed = n / fps * speed
        im = base("LIVE RUN REPLAY  /  2026.09.27", "在小红书上查找总结最近适合 staycation 的地方")
        d = ImageDraw.Draw(im)
        d.text((2160, 50), f"统一 {speed}× 回放", font=font(32), fill=INK)
        for side, arm in enumerate(arms):
            arm.draw(im, side, elapsed)
        d.text((64, 1360), "真实任务页画面 · 计时显示原始秒数 · API 重试与等待计入耗时 · 成本为已返回 usage 的累计估算", font=font(26), fill=MUTED)
        emit(im)
        if n % 80 == 0:
            print(f"Rendered comparison t={elapsed:.1f}s", flush=True)
    emit(cards[1], 15)
    emit(cards[2], 18)
    if len(cards) > 3:
        emit(cards[3], 14)
    process.stdin.close()
    if process.wait():
        raise RuntimeError("Video encoding failed")
    (args.output / "video-manifest.json").write_text(json.dumps({
        "video": str(target.resolve()), "dimensions": [W, H], "output_fps": 24,
        "composition_fps": fps, "replay_speed": speed,
        "duration_s": 40 + (14 if len(cards) > 3 else 0) + (math.ceil(race_seconds * fps) + 1) / fps,
        "source": "actual archived task-tab screenshots; no invented actions or historical task substitutions",
        "arms": [{"name": a.title, "elapsed_s": a.duration, "known_cost_usd": a.cost,
                  "unknown_usage_attempts": a.unknown, "report": str((a.folder / 'report.json').resolve())} for a in arms],
    }, ensure_ascii=False, indent=2) + "\n")
    print(target.resolve(), flush=True)


if __name__ == "__main__":
    main()
