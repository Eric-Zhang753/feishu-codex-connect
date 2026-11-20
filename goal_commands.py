"""Feishu commands for Codex's native persisted thread goals."""
import asyncio
import json

ALIASES = {'/goal_edit': 'edit', '/goal_pause': 'pause', '/goal_resume': 'resume', '/goal_clear': 'clear'}
STATUS = {'active': '运行中', 'paused': '已暂停', 'blocked': '等待处理',
          'usageLimited': '达到用量限制', 'budgetLimited': '达到预算限制', 'complete': '已完成'}


class GoalCommands:
    def __init__(self, bridge):
        self.bridge = bridge
        self.cache = {}
        self.locks = {}

    @staticmethod
    def parse(command, argument):
        argument = argument.strip()
        if command in ALIASES:
            action = ALIASES[command]
            if action != 'edit' and argument:
                raise ValueError('此命令不需要参数。')
            return action, argument
        if not argument:
            return 'status', ''
        parts = argument.split(maxsplit=1)
        action = parts[0].lower()
        if action in ('pause', 'resume', 'clear', 'status'):
            if len(parts) > 1:
                raise ValueError('此命令不需要额外参数。')
            return action, ''
        if action in ('edit', 'set'):
            return action, parts[1].strip() if len(parts) > 1 else ''
        return 'set', argument

    def mark_managed(self, thread, chat):
        self.bridge.state.set_setting('goal-thread:' + thread, json.dumps({'chat_id': chat}))

    def is_managed(self, thread):
        return bool(thread and self.bridge.state.setting('goal-thread:' + thread))

    def notice(self, params):
        thread = params.get('threadId')
        if thread:
            self.cache[thread] = params.get('goal')

    async def refresh(self, thread):
        result = await self.bridge.codex.request('thread/goal/get', {'threadId': thread})
        self.cache[thread] = result.get('goal')
        return self.cache[thread]

    @staticmethod
    def describe(goal):
        if not goal:
            return '当前会话没有目标。用 `/goal 目标内容` 开启目标模式。'
        lines = ['目标状态：' + STATUS.get(goal.get('status'), '未知'), '目标：' + goal['objective']]
        used = goal.get('tokensUsed', 0)
        budget = goal.get('tokenBudget')
        lines.append('累计 token：' + f'{used:,}' + (f' / {budget:,}' if budget is not None else ''))
        seconds = goal.get('timeUsedSeconds', 0)
        if seconds:
            lines.append(f'累计执行：{seconds // 60} 分 {seconds % 60} 秒')
        return '\n\n'.join(lines)

    def progress_title(self, thread):
        goal = self.cache.get(thread)
        status = (goal or {}).get('status')
        return {'active': '目标进展', 'paused': '目标已暂停', 'complete': '目标已完成',
                'blocked': '目标等待处理', 'usageLimited': '目标达到用量限制',
                'budgetLimited': '目标达到预算限制'}.get(status, '目标会话本轮结果')

    def notification_status(self, thread):
        status = (self.cache.get(thread) or {}).get('status')
        return {'active': '执行中', 'paused': '已暂停', 'complete': '已完成',
                'blocked': '待处理', 'usageLimited': '用量受限',
                'budgetLimited': '预算受限'}.get(status, '本轮结束')

    async def handle(self, message, command, argument):
        chat = message['chat_id']
        try:
            action, objective = self.parse(command, argument)
        except ValueError as exc:
            await self.bridge.reply(chat, str(exc))
            return
        if action in ('set', 'edit') and (not objective or len(objective) > 4000):
            await self.bridge.reply(chat, '请提供 1–4000 字的目标。\n设置：`/goal 目标内容`\n修改：`/goal_edit 新目标`')
            return
        async with self.locks.setdefault(chat, asyncio.Lock()):
            thread = self.bridge.state.thread(chat)
            if not thread and action != 'set':
                await self.bridge.reply(chat, '当前尚未选择会话或设置目标。先 `/use 会话编号`，或 `/goal 目标内容` 新建。')
                return
            await self.bridge.ensure_codex()
            if action == 'set':
                thread = await self.bridge.prepare_chat_thread(chat)
            current = await self.refresh(thread)
            if action == 'status':
                await self.bridge.reply(chat, self.describe(current),
                                        notice=self.notification_status(thread) if current else '未设目标')
                return
            if action != 'set' and not current:
                await self.bridge.reply(chat, self.describe(None))
                return
            if action == 'resume' and current.get('status') == 'complete':
                await self.bridge.reply(chat, '目标已经完成。用 `/goal 新目标` 开启下一目标。', notice='已完成')
                return
            if action in ('pause', 'resume'):
                desired = 'paused' if action == 'pause' else 'active'
                if current.get('status') == desired:
                    await self.bridge.reply(chat, '目标已经暂停。' if desired == 'paused' else '目标已经在运行。',
                                            notice='已暂停' if desired == 'paused' else '执行中')
                    return
            if action != 'set':
                # Attach to the same live backend before mutating goal control;
                # never fork a locked conversation into a different thread.
                thread = await self.bridge.prepare_chat_thread(chat)
            self.mark_managed(thread, chat)
            if action == 'clear':
                await self.bridge.codex.request('thread/goal/clear', {'threadId': thread, 'origin': 'user'})
                self.cache[thread] = None
                await self.bridge.reply(chat, '目标已清除，后续不再按该目标自动续跑。', notice='已清除')
                return
            params = {'threadId': thread, 'origin': 'user'}
            if action == 'set':
                params.update(objective=objective, status='active')
            elif action == 'edit':
                params.update(objective=objective, status='active' if current['status'] == 'active' else 'paused')
            else:
                params['status'] = 'paused' if action == 'pause' else 'active'
            result = await self.bridge.codex.request('thread/goal/set', params)
            goal = result['goal']
            self.cache[thread] = goal
            title = {'set': '目标模式已开启', 'edit': '目标已修改',
                     'pause': '已暂停目标自动续跑', 'resume': '目标已继续'}[action]
            if action == 'edit' and goal.get('status') == 'paused':
                title += '，保持暂停；用 /goal_resume 继续'
            await self.bridge.reply(chat, title + '\n\n' + self.describe(goal), notice=self.notification_status(thread))
