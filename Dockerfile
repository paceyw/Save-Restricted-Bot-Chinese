FROM python:3.10-slim
RUN apt-get update && apt-get install -y git curl ffmpeg python3-pip wget bash fontconfig && apt-get clean && rm -rf /var/lib/apt/lists/*

# Chinese font for subtitle burn-in (libass). The bot runs as uid 1000
# (bot-user), so the font must be world-readable and fontconfig needs a
# writable cache dir — bake a system-wide cache at build time so runtime
# needs no writes at all.
ADD https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Bold.otf /tmp/NotoSansCJKsc-Bold.otf
RUN mkdir -p /usr/share/fonts/opentype/noto \
    && install -m 0644 /tmp/NotoSansCJKsc-Bold.otf /usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf \
    && rm /tmp/NotoSansCJKsc-Bold.otf \
    && mkdir -p /var/cache/fontconfig \
    && fc-cache -fs \
    && fc-list | grep -i "noto sans cjk sc" \
    && chmod -R a+rX /var/cache/fontconfig
ENV XDG_CACHE_HOME=/tmp/.fcache
WORKDIR /app
COPY requirements.txt .

RUN pip3 install wheel
RUN pip3 install --no-cache-dir -U -r requirements.txt
COPY . .
EXPOSE 5000

CMD flask run -h 0.0.0.0 -p 5000 & python3 main.py
