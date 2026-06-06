from datetime import datetime

from config import ADMIN_IDS, TIMEZONE, logger
from database import format_db_datetime, get_all_active_tasks


async def check_overdue_tasks(app):
    """Проверяет просроченные задачи и отправляет уведомления."""
    now = datetime.now(TIMEZONE)
    tasks = get_all_active_tasks()

    for task in tasks:
        due_date = task.get("due_date")
        if due_date and due_date < now and task.get("progress", 0) < 100:
            try:
                await app.bot.send_message(
                    chat_id=task["assigned_to"],
                    text=(
                        f"⚠️ Задача #{task['id']} «{task['title']}» просрочена!\n"
                        f"Срок был: {format_db_datetime(due_date)}"
                    ),
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить исполнителя {task['assigned_to']}: {e}")

            for admin_id in ADMIN_IDS:
                try:
                    await app.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"🚨 Задача #{task['id']} «{task['title']}» просрочена.\n"
                            f"Ответственный: {task['assigned_to']}"
                        ),
                    )
                except Exception as e:
                    logger.error(f"Не удалось уведомить администратора {admin_id}: {e}")


def schedule_task_checker(app):
    """Запускает проверку задач каждые 10 минут."""
    job_queue = app.job_queue
    if not job_queue:
        logger.warning("JobQueue не запущен. Установите python-telegram-bot[job-queue]")
        return

    async def callback(_):
        await check_overdue_tasks(app)

    job_queue.run_repeating(callback, interval=600, first=10)
    logger.info("Планировщик проверки задач запущен")
