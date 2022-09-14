FROM gorialis/discord.py:minimal

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY bot.py ./
COPY celestenet.py ./

RUN useradd appuser && chown -R appuser /app
USER appuser

#CMD ["bash"]
CMD ["python", "bot.py"]