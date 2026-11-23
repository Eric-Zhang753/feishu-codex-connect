import asyncio
import base64
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from test_bridge import b, config
from result_attachments import MAX_IMAGE_BYTES, extract_markdown_attachments, read_local_attachment


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXxkAAAAASUVORK5CYII=')
PDF = b'%PDF-1.4\n% mock document\n%%EOF\n'


class ResultImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.bridge = b.Bridge(dict(config(), project_dir=str(self.workspace), shared_socket='/tmp/test.sock'),
                               self.root)
        self.addCleanup(self.bridge.state.db.close)
        self.addAsyncCleanup(self.bridge.feishu.client.aclose)
        self.bridge.reply = AsyncMock(return_value=True)
        self.bridge.feishu.upload_image = AsyncMock(return_value='image-key')
        self.bridge.feishu.upload_file = AsyncMock(return_value='file-key')
        self.bridge.feishu.send_message = AsyncMock()
        self.bridge.state.select_thread('chat', 'thread', str(self.workspace))

    def mark_chat(self, chat_type):
        self.bridge.state.receive({'message_id': 'feishu-message', 'chat_id': 'chat', 'user_id': 'owner',
            'chat_type': chat_type, 'text': '/use thread', 'created': time.time()})

    async def test_shared_result_sends_generated_and_markdown_image_once(self):
        self.mark_chat('p2p')
        path = self.workspace / 'plot (1).png'
        path.write_bytes(PNG)
        image = {'id': 'image', 'type': 'imageGeneration', 'status': 'completed', 'savedPath': str(path)}
        final = {'id': 'answer', 'type': 'agentMessage', 'phase': 'final_answer',
                 'text': '![结果](<plot (1).png>)'}
        await self.bridge.on_codex({'method': 'turn/started', 'params': {'threadId': 'thread',
            'turn': {'id': 'turn', 'status': 'inProgress'}}})
        await self.bridge.on_codex({'method': 'item/completed', 'params': {'threadId': 'thread',
            'turnId': 'turn', 'item': image}})
        await self.bridge.on_codex({'method': 'turn/completed', 'params': {'threadId': 'thread',
            'turn': {'id': 'turn', 'status': 'completed', 'items': [image, final]}}})
        self.bridge.feishu.upload_image.assert_awaited_once_with(PNG, 'png', 'image/png')
        self.bridge.feishu.send_message.assert_awaited_once_with('chat', 'image', {'image_key': 'image-key'})
        self.assertTrue(self.bridge.reply.await_args.args[1].endswith('结果'))

    async def test_group_and_outside_workspace_images_are_not_uploaded(self):
        outside = self.root / 'outside.png'
        outside.write_bytes(PNG)
        (self.workspace / 'link.png').symlink_to(outside)
        self.assertEqual(read_local_attachment(Path('link.png'), self.workspace, limit=MAX_IMAGE_BYTES), b'')
        self.mark_chat('group')
        text = '![图](plot.png)'
        (self.workspace / 'plot.png').write_bytes(PNG)
        await self.bridge.reply_result('chat', text, {'thread_id': 'thread'}, notice='已完成')
        self.bridge.reply.assert_awaited_once_with('chat', text, notice='已完成')
        self.bridge.feishu.upload_image.assert_not_awaited()

    async def test_missing_upload_permission_still_delivers_result_text(self):
        self.bridge.feishu.upload_image.side_effect = b.FeishuResourcePermissionError()
        image = {'id': 'image', 'type': 'imageGeneration', 'status': 'completed',
                 'result': base64.b64encode(PNG).decode()}
        await self.bridge.reply_result('chat', '正文', {'chat_type': 'p2p', 'thread_id': 'thread',
            'images': {'image': image}}, notice='已完成')
        self.assertEqual(self.bridge.reply.await_args_list[0].args[1], '正文')
        self.assertIn('im:resource', self.bridge.reply.await_args.args[1])
        self.bridge.feishu.send_message.assert_not_awaited()

    async def test_final_answer_pdf_is_sent_with_its_original_filename(self):
        (self.workspace / '项目速览.pdf').write_bytes(PDF)
        await self.bridge.reply_result('chat', '[下载速览](项目速览.pdf)',
            {'chat_type': 'p2p', 'thread_id': 'thread'}, notice='已完成')
        self.bridge.feishu.upload_file.assert_awaited_once_with('项目速览.pdf', PDF, 'pdf', 'application/pdf')
        self.bridge.feishu.send_message.assert_awaited_once_with('chat', 'file', {'file_key': 'file-key'})
        self.bridge.feishu.upload_image.assert_not_awaited()

    def test_markdown_keeps_examples_and_remote_urls_clickable(self):
        text = '`![示例](a.png)`\n~~~\n![例子](b.png)\n~~~\n![网络图](https://example.org/image.png)\n'
        display, paths = extract_markdown_attachments(text)
        self.assertEqual(display, text.replace('![网络图]', '[网络图]'))
        self.assertEqual(paths, ())


class ImageTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_upload_and_native_message_use_the_bot_apis(self):
        requests = []

        def handler(request):
            if 'tenant_access_token' in request.url.path:
                return b.httpx.Response(200, json={'code': 0, 'tenant_access_token': 'token', 'expire': 7200})
            requests.append(request)
            if request.url.path.endswith('/images'):
                return b.httpx.Response(200, json={'code': 0, 'data': {'image_key': 'image-key'}})
            if request.url.path.endswith('/files'):
                return b.httpx.Response(200, json={'code': 0, 'data': {'file_key': 'file-key'}})
            return b.httpx.Response(200, json={'code': 0, 'data': {'message_id': 'message'}})

        client = b.Feishu(config())
        await client.client.aclose()
        client.client = b.httpx.AsyncClient(base_url='https://open.feishu.cn', transport=b.httpx.MockTransport(handler))
        self.addAsyncCleanup(client.client.aclose)
        key = await client.upload_image(PNG, 'png', 'image/png')
        self.assertEqual(await client.send_message('chat', 'image', {'image_key': key}), 'message')
        self.assertIn(b'name="image_type"', requests[0].content)
        self.assertIn(PNG, requests[0].content)
        message = json.loads(requests[1].content)
        self.assertEqual(message['msg_type'], 'image')
        self.assertEqual(json.loads(message['content']), {'image_key': 'image-key'})
        key = await client.upload_file('sample.pdf', PDF, 'pdf', 'application/pdf')
        self.assertEqual(await client.send_message('chat', 'file', {'file_key': key}), 'message')
        self.assertIn(b'name="file_type"\r\n\r\npdf', requests[2].content)
        self.assertIn(b'name="file_name"\r\n\r\nsample.pdf', requests[2].content)
        self.assertIn(PDF, requests[2].content)
        message = json.loads(requests[3].content)
        self.assertEqual(message['msg_type'], 'file')
        self.assertEqual(json.loads(message['content']), {'file_key': 'file-key'})
