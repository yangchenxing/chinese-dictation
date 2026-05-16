#!/usr/bin/env python3
"""中文听写程序 - 启动后通过网页交互。"""

import asyncio
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

import edge_tts
from rich.console import Console

# ============ 配置 ============
WORDS_DIR = Path("./words")
AUDIO_DIR = Path("./audio")
INTERVAL_1 = 1          # 两遍之间的间隔（秒）
INTERVAL_2 = 1          # 每个字符的等待时间（秒）
INTERVAL_3 = 0          # 额外等待时间（秒）
WEB_PORT = 8765         # Web 服务端口

TTS_VOICE = "zh-CN-XiaoxiaoNeural"
TTS_RATE = "-30%"

console = Console()

# ============ 全局状态 ============

# 音频生成进度状态
generation_state = {
    "running": False,
    "total": 0,
    "completed": 0,
    "current_word": "",
    "error": None,
}

# 当前选中的词汇表单词列表
current_words: list[str] = []


# ============ 词汇表扫描 ============

def list_word_files(directory: Path) -> list[Path]:
    """递归列出目录下所有 .txt 词汇表文件。"""
    if not directory.exists():
        return []
    files = sorted(directory.rglob("*.txt"))
    return files


def read_words(file_path: Path) -> list[str]:
    """读取词汇表文件，返回单词列表。"""
    words = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            word = line.strip()
            if word:
                words.append(word)
    return words


# ============ TTS 语音生成 ============

def get_audio_path(word: str) -> Path:
    """获取单词对应的音频文件路径。"""
    safe_name = word.replace("/", "_").replace("\\", "_")
    return AUDIO_DIR / f"{safe_name}.mp3"


async def _generate_audio_async(word: str, path: Path) -> None:
    """使用 edge-tts 异步生成单个单词的音频。"""
    text_with_breaks = "，".join(word) + "，"
    communicate = edge_tts.Communicate(
        text_with_breaks,
        voice=TTS_VOICE,
        rate=TTS_RATE,
    )
    await communicate.save(str(path))


async def ensure_audio_async(words: list[str]) -> None:
    """异步生成音频并更新进度状态。"""
    global generation_state
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    missing = [w for w in words if not get_audio_path(w).exists()]

    if not missing:
        generation_state["running"] = False
        generation_state["completed"] = generation_state["total"]
        return

    generation_state["total"] = len(missing)
    generation_state["completed"] = 0
    generation_state["running"] = True
    generation_state["error"] = None

    for i, word in enumerate(missing, 1):
        generation_state["current_word"] = word
        path = get_audio_path(word)
        try:
            await _generate_audio_async(word, path)
            generation_state["completed"] = i
        except Exception as e:
            generation_state["error"] = f"{word}: {e}"
            console.print(f"[red]生成失败: {word} ({e})[/red]")

    generation_state["running"] = False
    generation_state["current_word"] = ""


def start_audio_generation(words: list[str]) -> None:
    """在后台线程中启动音频生成。"""
    global generation_state
    generation_state = {
        "running": True,
        "total": len([w for w in words if not get_audio_path(w).exists()]),
        "completed": 0,
        "current_word": "",
        "error": None,
    }

    def run_async():
        asyncio.run(ensure_audio_async(words))

    thread = threading.Thread(target=run_async, daemon=True)
    thread.start()


# ============ WSL 检测与浏览器打开 ============

def is_wsl() -> bool:
    """检测当前是否在 WSL 环境中运行。"""
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8") as f:
            version_info = f.read().lower()
            if "microsoft" in version_info or "wsl" in version_info:
                return True
    except Exception:
        pass
    return False


def open_browser(url: str) -> None:
    """在合适的平台打开浏览器。"""
    if is_wsl():
        try:
            subprocess.run(
                ["powershell.exe", "-Command", f"Start-Process '{url}'"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            try:
                subprocess.run(
                    ["cmd.exe", "/c", "start", url],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                console.print("[yellow]未能自动打开浏览器，请手动访问上述链接。[/yellow]")
                return
    webbrowser.open(url)


# ============ Web 页面模板 ============

HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>中文听写</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body {
    width: 100%;
    height: 100%;
    overflow: hidden;
    font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    background: #1a1a2e;
    color: #fff;
}

/* ===== 词汇表选择界面 ===== */
#select-screen {
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 4vh 4vw;
    overflow-y: auto;
}
#select-title {
    font-size: min(6vw, 5vh);
    font-weight: bold;
    margin-bottom: 3vh;
    color: #ffd700;
}
.file-list {
    width: 100%;
    max-width: 800px;
    display: flex;
    flex-direction: column;
    gap: 2vh;
}
.file-item {
    background: #0f3460;
    border-radius: 1.5vh;
    padding: 2vh 3vw;
    font-size: min(4vw, 3vh);
    cursor: pointer;
    transition: background 0.2s ease, transform 0.1s ease;
    display: flex;
    align-items: center;
    gap: 2vw;
}
.file-item:hover {
    background: #1a4a7a;
    transform: scale(1.01);
}
.file-item:active {
    transform: scale(0.98);
}
.file-icon { font-size: min(5vw, 4vh); }
.file-name { flex: 1; }

/* ===== 进度界面 ===== */
#progress-screen {
    display: none;
    width: 100%;
    height: 100%;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 4vh 4vw;
}
#progress-label {
    font-size: min(6vw, 5vh);
    font-weight: bold;
    margin-bottom: 3vh;
    color: #ffd700;
}
#progress-bar-container {
    width: 90%;
    max-width: 800px;
    height: 5vh;
    background: #333;
    border-radius: 2.5vh;
    overflow: hidden;
    margin-bottom: 2vh;
}
#progress-bar {
    width: 0%;
    height: 100%;
    background: linear-gradient(90deg, #4caf50, #8bc34a);
    transition: width 0.3s ease;
}
#progress-percent {
    font-size: min(10vw, 8vh);
    font-weight: bold;
    color: #4caf50;
}
#progress-current {
    font-size: min(4vw, 3vh);
    color: #aaa;
    margin-top: 1vh;
}

/* ===== 听写界面 ===== */
#dictation-screen {
    display: none;
    width: 100%;
    height: 100%;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    user-select: none;
}
#main-text {
    font-size: min(40vw, 80vh);
    font-weight: bold;
    line-height: 1;
    text-align: center;
    white-space: nowrap;
}
#progress-text {
    display: none;
    font-size: min(20vw, 80vh);
    font-weight: bold;
    line-height: 1;
    text-align: center;
    white-space: nowrap;
}
#countdown-text {
    display: none;
    font-size: min(10vw, 15vh);
    margin-top: 4vh;
    color: #ffd700;
    font-weight: bold;
}
.green-bg {
    background: #4caf50 !important;
}
#word-grid {
    display: none;
    width: 100%;
    height: 100%;
    overflow-y: auto;
    background: #16213e;
    padding: 2vh 2vw;
}
.grid-container {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    grid-auto-rows: 1fr;
    gap: 1.5vh 1.5vw;
    width: 100%;
}
.grid-item {
    display: flex;
    align-items: center;
    justify-content: center;
    background: #0f3460;
    border-radius: 1.5vh;
    font-weight: bold;
    text-align: center;
    overflow: hidden;
    aspect-ratio: 4 / 3;
    padding: 1vw;
}
</style>
</head>
<body>

<!-- 词汇表选择 -->
<div id="select-screen">
    <div id="select-title">选择词汇表</div>
    <div class="file-list" id="file-list"></div>
</div>

<!-- 进度条 -->
<div id="progress-screen">
    <div id="progress-label">正在准备语音...</div>
    <div id="progress-bar-container">
        <div id="progress-bar"></div>
    </div>
    <div id="progress-percent">0%</div>
    <div id="progress-current"></div>
</div>

<!-- 听写 -->
<div id="dictation-screen">
    <div id="main-text">开始</div>
    <div id="progress-text"></div>
    <div id="countdown-text"></div>
</div>

<!-- 结果网格 -->
<div id="word-grid">
    <div class="grid-container" id="grid-container"></div>
</div>

<script>
const interval1 = %(INTERVAL1)d;
const interval2 = %(INTERVAL2)d;
const interval3 = %(INTERVAL3)d;
const audioDir = "audio";

const selectScreen = document.getElementById('select-screen');
const fileList = document.getElementById('file-list');
const progressScreen = document.getElementById('progress-screen');
const progressBar = document.getElementById('progress-bar');
const progressPercent = document.getElementById('progress-percent');
const progressCurrent = document.getElementById('progress-current');
const dictationScreen = document.getElementById('dictation-screen');
const mainText = document.getElementById('main-text');
const progressText = document.getElementById('progress-text');
const countdownText = document.getElementById('countdown-text');
const wordGrid = document.getElementById('word-grid');
const gridContainer = document.getElementById('grid-container');

let words = [];
let isPlaying = false;
let isFinished = false;

// ===== 词汇表选择 =====

async function loadWordFiles() {
    const res = await fetch('/api/wordfiles');
    const files = await res.json();
    fileList.innerHTML = '';
    if (files.length === 0) {
        fileList.innerHTML = '<div style="text-align:center;color:#aaa;">暂无词汇表文件</div>';
        return;
    }
    files.forEach(file => {
        const div = document.createElement('div');
        div.className = 'file-item';
        div.innerHTML = '<span class="file-icon">📄</span><span class="file-name">' + escapeHtml(file.name) + '</span>';
        div.addEventListener('click', () => selectFile(file.path));
        fileList.appendChild(div);
    });
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function selectFile(path) {
    selectScreen.style.display = 'none';
    progressScreen.style.display = 'flex';
    const res = await fetch('/api/select', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: path }),
    });
    const data = await res.json();
    if (data.error) {
        alert(data.error);
        selectScreen.style.display = 'flex';
        progressScreen.style.display = 'none';
        return;
    }
    words = data.words;
    pollProgress();
}

// ===== 进度轮询 =====

async function pollProgress() {
    try {
        const res = await fetch('/api/progress');
        const data = await res.json();
        const percent = data.total > 0 ? Math.round((data.completed / data.total) * 100) : 100;
        progressBar.style.width = percent + '%';
        progressPercent.textContent = percent + '%';
        progressCurrent.textContent = data.current_word ? '正在生成: ' + data.current_word : '';

        if (data.running) {
            setTimeout(pollProgress, 500);
        } else {
            if (data.error) {
                alert('生成出错: ' + data.error);
                selectScreen.style.display = 'flex';
                progressScreen.style.display = 'none';
            } else {
                setTimeout(() => startDictationScreen(), 300);
            }
        }
    } catch (e) {
        setTimeout(pollProgress, 500);
    }
}

// ===== 听写界面 =====

function startDictationScreen() {
    progressScreen.style.display = 'none';
    dictationScreen.style.display = 'flex';
}

dictationScreen.addEventListener('click', () => {
    if (!isPlaying && !isFinished) {
        startDictation();
    } else if (isFinished) {
        showWordGrid();
    }
});

function getAudioUrl(word) {
    return audioDir + '/' + encodeURIComponent(word) + '.mp3';
}

function playAudio(word) {
    return new Promise((resolve) => {
        const audio = new Audio(getAudioUrl(word));
        audio.onended = resolve;
        audio.onerror = () => {
            console.error('播放失败:', word);
            resolve();
        };
        audio.play().catch(() => resolve());
    });
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function startDictation() {
    isPlaying = true;
    mainText.style.display = 'none';
    progressText.style.display = 'block';

    for (let i = 0; i < words.length; i++) {
        const word = words[i];
        progressText.textContent = (i + 1) + '/' + words.length;

        // 第一遍
        await playAudio(word);
        await sleep(interval1 * 1000);
        // 第二遍
        await playAudio(word);

        // 等待时间
        const waitSeconds = word.length * interval2 + interval3;
        countdownText.style.display = 'block';
        for (let s = waitSeconds; s > 0; s--) {
            countdownText.textContent = s + '秒';
            await sleep(1000);
        }
        countdownText.style.display = 'none';
    }

    // 完成
    isPlaying = false;
    isFinished = true;
    progressText.style.display = 'none';
    dictationScreen.classList.add('green-bg');
    mainText.textContent = '完成';
    mainText.style.display = 'block';
}

function showWordGrid() {
    dictationScreen.style.display = 'none';
    wordGrid.style.display = 'block';

    gridContainer.innerHTML = '';
    words.forEach(word => {
        const div = document.createElement('div');
        div.className = 'grid-item';
        const len = Math.max(word.length, 1);
        const fontSize = 'min(' + (24 / len) + 'vw, 11vh)';
        div.style.fontSize = fontSize;
        div.textContent = word;
        gridContainer.appendChild(div);
    });
}

// 启动
loadWordFiles();
</script>
</body>
</html>
'''


# ============ Web 服务 ============

class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            html = HTML_TEMPLATE \
                .replace("%(INTERVAL1)d", str(INTERVAL_1)) \
                .replace("%(INTERVAL2)d", str(INTERVAL_2)) \
                .replace("%(INTERVAL3)d", str(INTERVAL_3))
            self.wfile.write(html.encode("utf-8"))
            return

        if self.path == "/api/wordfiles":
            self._send_json(200, _get_wordfiles())
            return

        if self.path == "/api/progress":
            self._send_json(200, {
                "running": generation_state["running"],
                "total": generation_state["total"],
                "completed": generation_state["completed"],
                "current_word": generation_state["current_word"],
                "error": generation_state["error"],
            })
            return

        super().do_GET()

    def do_POST(self):
        if self.path == "/api/select":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                data = json.loads(body)
                file_path = Path(data.get("path", ""))
            except Exception:
                self._send_json(400, {"error": "无效的请求"})
                return

            if not file_path.exists() or not file_path.is_file():
                self._send_json(400, {"error": "文件不存在"})
                return

            words = read_words(file_path)
            if not words:
                self._send_json(400, {"error": "词汇表为空"})
                return

            global current_words
            current_words = words

            # 启动后台音频生成
            start_audio_generation(words)

            self._send_json(200, {"words": words})
            return

        self.send_error(404)

    def _send_json(self, status: int, data: dict) -> None:
        self.send_response(status)
        self.send_header("Content-type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        pass


def _get_wordfiles() -> list[dict]:
    """获取词汇表文件列表。"""
    files = list_word_files(WORDS_DIR)
    return [
        {"name": f.name, "path": str(f)}
        for f in files
    ]


def start_server(open_browser_flag: bool = True) -> None:
    """启动 Web 服务器。"""
    ThreadingHTTPServer.allow_reuse_address = True
    with ThreadingHTTPServer(("", WEB_PORT), RequestHandler) as httpd:
        url = f"http://localhost:{WEB_PORT}"
        console.print(f"[green]Web 服务已启动: {url}[/green]")
        if open_browser_flag:
            console.print("[yellow]正在打开浏览器...[/yellow]")
            open_browser(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            console.print("\n[yellow]服务已停止。[/yellow]")


# ============ 主入口 ============

def main() -> None:
    if not WORDS_DIR.exists():
        console.print(f"[red]词汇表目录 {WORDS_DIR} 不存在，请先创建并放入词汇表文件。[/red]")
        sys.exit(1)

    start_server()


if __name__ == "__main__":
    main()
