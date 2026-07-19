python3 -m pip install -U discord.py --break-system-packages

sudo cp vps-bot.service /etc/systemd/system/vps-bot.service

sudo systemctl daemon-reload

sudo systemctl enable vps-bot.service

sudo systemctl start vps-bot.service

sed -i 's|token = ""|token = "YOUR_BOT_TOKEN"|g' bot.py
