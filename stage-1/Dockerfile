FROM python:3.12-alpine

WORKDIR /app

COPY time_utils.py validation.py passwords.py state_core.py operations.py app.py /app/

EXPOSE 8080
ENV PORT=8080

CMD ["python", "-u", "app.py"]
