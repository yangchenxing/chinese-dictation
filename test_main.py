#!/usr/bin/env python3
"""中文听写程序单元测试。

Use Case: 用户要听写 test 词汇表

该 Use Case 中前端产生的 HTTP Request 序列：
1. GET  /              → 加载首页 HTML
2. GET  /api/wordfiles → 获取词汇表文件列表
3. POST /api/select    → 选择 words/test.txt，服务端返回单词列表并后台启动音频生成
4. GET  /api/progress  → 轮询音频生成进度（多次，直到 running=false）
5. GET  /audio/天地人.mp3 → 听写时播放第一遍
6. GET  /audio/天地人.mp3 → 听写时播放第二遍
7. GET  /audio/你我他.mp3 → 听写时播放第一遍
8. GET  /audio/你我他.mp3 → 听写时播放第二遍
"""

import json
import shutil
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

# 把项目根目录加入路径，确保能导入 main
sys.path.insert(0, str(Path(__file__).parent))

import main as app


# ---------- mock 音频生成，避免测试中调用 edge-tts ----------

async def _mock_generate_audio_async(word: str, path: Path) -> None:
    """快速生成一个假的 mp3 文件（同步写入，无需网络）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"FAKE_MP3_" + word.encode("utf-8"))


# ---------- 测试基类 ----------

class DictationServerTestCase(unittest.TestCase):
    """测试 HTTP 服务器的完整 Use Case。"""

    @classmethod
    def setUpClass(cls):
        # 1. 清理旧音频，避免历史数据干扰
        shutil.rmtree(app.AUDIO_DIR, ignore_errors=True)

        # 2. Mock 掉 edge-tts，让音频生成瞬间完成
        cls._original_generate = app._generate_audio_async
        app._generate_audio_async = _mock_generate_audio_async

        # 3. 使用随机端口，避免和已运行的服务冲突
        cls.test_port = 18765
        app.WEB_PORT = cls.test_port

        # 4. 重置全局状态
        app.generation_state = {
            "running": False,
            "total": 0,
            "completed": 0,
            "current_word": "",
            "error": None,
        }
        app.current_words = []

        # 5. 在后台线程启动 ThreadingHTTPServer
        cls.server = app.ThreadingHTTPServer(("", cls.test_port), app.RequestHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever)
        cls.server_thread.daemon = True
        cls.server_thread.start()
        cls.base_url = f"http://localhost:{cls.test_port}"

        # 6. 给服务器一点启动时间
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        # 1. 关闭服务器
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2)

        # 2. 恢复 mock
        app._generate_audio_async = cls._original_generate

        # 3. 清理测试音频
        shutil.rmtree(app.AUDIO_DIR, ignore_errors=True)

    def _get(self, path: str) -> urllib.request.addinfourl:
        """发送 GET 请求。"""
        req = urllib.request.Request(f"{self.base_url}{path}")
        return urllib.request.urlopen(req)

    def _post(self, path: str, payload: dict) -> urllib.request.addinfourl:
        """发送 POST 请求（JSON body）。"""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(req)

    # ---------- 用例 1: 加载首页 ----------
    def test_01_get_index(self):
        """UC-Step-1: 用户打开浏览器访问首页，应返回包含"选择词汇表"的 HTML。"""
        resp = self._get("/")
        self.assertEqual(resp.status, 200)
        html = resp.read().decode("utf-8")
        self.assertIn("选择词汇表", html)
        self.assertIn("/api/wordfiles", html)

    # ---------- 用例 2: 获取词汇表列表 ----------
    def test_02_get_wordfiles(self):
        """UC-Step-2: 前端获取词汇表列表，应返回包含 test.txt 的 JSON 数组。"""
        resp = self._get("/api/wordfiles")
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode("utf-8"))
        self.assertIsInstance(data, list)
        self.assertTrue(
            any(f["name"] == "test.txt" for f in data),
            f"词汇表列表中应包含 test.txt，实际返回: {data}",
        )
        # 校验字段结构
        for f in data:
            self.assertIn("name", f)
            self.assertIn("path", f)

    # ---------- 用例 3: 选择 test 词汇表 ----------
    def test_03_post_select_test(self):
        """UC-Step-3: 用户点击 test.txt，前端 POST /api/select。

        服务端应：
        - 返回 200 及单词列表 ["天地人", "你我他"]
        - 在后台启动音频生成
        """
        # 先确保没有残留音频
        shutil.rmtree(app.AUDIO_DIR, ignore_errors=True)
        app.generation_state = {
            "running": False,
            "total": 0,
            "completed": 0,
            "current_word": "",
            "error": None,
        }

        resp = self._post("/api/select", {"path": "words/test.txt"})
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(data["words"], ["天地人", "你我他"])

        # 后台生成已启动，稍等片刻让 mock 完成
        time.sleep(0.3)

        # 验证全局状态
        self.assertFalse(app.generation_state["running"])
        self.assertEqual(app.generation_state["completed"], 2)
        self.assertEqual(app.generation_state["total"], 2)

        # 验证音频文件已生成
        self.assertTrue((app.AUDIO_DIR / "天地人.mp3").exists())
        self.assertTrue((app.AUDIO_DIR / "你我他.mp3").exists())

    # ---------- 用例 4: 进度轮询 ----------
    def test_04_get_progress(self):
        """UC-Step-4: 前端轮询 /api/progress，应返回合法的进度 JSON。"""
        resp = self._get("/api/progress")
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode("utf-8"))

        # 校验字段完整性
        self.assertIn("running", data)
        self.assertIn("total", data)
        self.assertIn("completed", data)
        self.assertIn("current_word", data)
        self.assertIn("error", data)

        # 校验数值关系
        self.assertIsInstance(data["running"], bool)
        self.assertIsInstance(data["total"], int)
        self.assertIsInstance(data["completed"], int)
        self.assertGreaterEqual(data["completed"], 0)
        self.assertGreaterEqual(data["total"], 0)

    # ---------- 用例 5: 播放音频（静态文件） ----------
    def test_05_get_audio_file(self):
        """UC-Step-5~8: 听写过程中前端请求 /audio/xxx.mp3。

        SimpleHTTPRequestHandler 应正确返回音频文件。
        """
        # 预先放置一个假音频文件
        app.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        fake_audio = app.AUDIO_DIR / "天地人.mp3"
        fake_audio.write_bytes(b"FAKE_MP3_DATA")

        resp = self._get("/audio/%E5%A4%A9%E5%9C%B0%E4%BA%BA.mp3")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.read(), b"FAKE_MP3_DATA")

    # ---------- 用例 6: 错误处理 —— 文件不存在 ----------
    def test_06_post_select_not_found(self):
        """UC-Error-1: 用户选择一个不存在的文件，应返回 400 错误。"""
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/api/select", {"path": "words/不存在.txt"})
        self.assertEqual(cm.exception.code, 400)
        err_body = json.loads(cm.exception.read().decode("utf-8"))
        self.assertIn("error", err_body)

    # ---------- 用例 7: 错误处理 —— 空词汇表 ----------
    def test_07_post_select_empty_words(self):
        """UC-Error-2: 用户选择一个空文件，应返回 400 错误。"""
        # 临时创建一个空文件
        empty_file = Path("words/_empty_test.txt")
        empty_file.write_text("\n\n\n", encoding="utf-8")
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self._post("/api/select", {"path": str(empty_file)})
            self.assertEqual(cm.exception.code, 400)
            err_body = json.loads(cm.exception.read().decode("utf-8"))
            self.assertEqual(err_body["error"], "词汇表为空")
        finally:
            empty_file.unlink(missing_ok=True)

    # ---------- 用例 8: 端到端 Use Case 完整流程 ----------
    def test_08_full_use_case(self):
        """UC-Full: 模拟"用户听写 test 词汇表"的完整 HTTP 交互。

        步骤：
        1. GET  /              → 首页
        2. GET  /api/wordfiles → 列表
        3. POST /api/select    → 选中 test.txt
        4. GET  /api/progress  → 轮询直到完成
        5. GET  /audio/天地人.mp3 → 播放
        6. GET  /audio/你我他.mp3 → 播放
        """
        # Step 1: 首页
        resp = self._get("/")
        self.assertEqual(resp.status, 200)

        # Step 2: 词汇表列表
        resp = self._get("/api/wordfiles")
        files = json.loads(resp.read().decode("utf-8"))
        test_file = next(f for f in files if f["name"] == "test.txt")
        self.assertIsNotNone(test_file)

        # Step 3: 选择词汇表
        shutil.rmtree(app.AUDIO_DIR, ignore_errors=True)
        resp = self._post("/api/select", {"path": test_file["path"]})
        self.assertEqual(resp.status, 200)
        select_data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(select_data["words"], ["天地人", "你我他"])

        # Step 4: 轮询进度直到完成（mock 生成很快，但这里显式轮询）
        for _ in range(20):
            resp = self._get("/api/progress")
            progress = json.loads(resp.read().decode("utf-8"))
            if not progress["running"]:
                break
            time.sleep(0.05)
        self.assertFalse(progress["running"])
        self.assertEqual(progress["completed"], 2)

        # Step 5~6: 播放音频
        for word in ["天地人", "你我他"]:
            encoded = urllib.parse.quote(word)
            resp = self._get(f"/audio/{encoded}.mp3")
            self.assertEqual(resp.status, 200)
            self.assertTrue(len(resp.read()) > 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
