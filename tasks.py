import logging
from datetime import datetime
from config import TIMEZONE, ADMIN_IDS, logger
from database import get_all_active_tasks

async def check_overdue_tasks(app):
    """Проверяет просроченные задачи и отправляет уведомления."""
    now = datetime.now(TIMEZONE)
    tasks = get_all_active_tasks()
    for task in tasks:
        # Если есть дедлайн, задача активна и прогресс меньше 100%
        if (task.get('due_date') and 
            task['due_date'] < now and 
            task.get('status') == 'active' and 
            task.get('progress', 0) < 100):
            
            # Уведомляем исполнителя
            try:
                await app.bot.send_message(
                    chat_id=task['assigned_to'],
                    text=f"⚠️ Задача #{task['id']} '{task['title']}' просрочена!\nСрок был: {task['due_date'].strftime('%d.%m.%Y %H:%M')}"
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить исполнителя {task['assigned_to']}: {e}")
            
            # Уведомляем администраторов
            for admin_id in ADMIN_IDS:
                try:
                    await app.bot.send_message(
                        chat_id=admin_id,
                        text=f"🚨 Задача #{task['id']} '{task['title']}' просрочена.\nОтветственный: {task['assigned_to']}"
                    )
                except:
                    pass

def check_all_tasks(app):
    """Заглушка для совместимости с импортом (функция не используется, но нужна)."""
    pass

def schedule_task_checker(app):
    """Запускает фоновый планировщик проверки задач (каждые 10 минут)."""
    job_queue = app.job_queue
    if job_queue:
        async def callback(_):
            await check_overdue_tasks(app)
        job_queue.run_repeating(callback, interval=600, first=10)
        logger.info("Планировщик проверки задач запущен")