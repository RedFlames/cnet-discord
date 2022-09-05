FROM gorialis/discord.py:minimal

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

CMD ["bash"]
#CMD ["python", "bot.py"]
