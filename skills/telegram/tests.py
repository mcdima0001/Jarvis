from telethon import TelegramClient

client = TelegramClient("session", 35, "ав")

client.start()

print("Клиент запущен!")

client.run_until_disconnected()