# main.py
import asyncio
import csv
import hashlib
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from pixivpy3 import AppPixivAPI
import aiohttp
from crawlee import Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.router import Router
from crawlee.storages import Dataset
import base64
from urllib.parse import urlparse
from config import (
    IMAGES_DIR, METADATA_FILE, 
    SUBREDDITS_HUMAN, SUBREDDITS_AI, 
    MAX_POSTS_PER_SUB, CIVITAI_LIMIT,
    TWITTER_TAGS, TWITTER_LIMIT,
    TWITTER_COOKIES,
    ARTSTATION_QUERIES_HUMAN, ARTSTATION_QUERIES_AI, ARTSTATION_LIMIT,     
    PIXIV_REFRESH_TOKEN, PIXIV_PROXY, PIXIV_LIMIT, PIXIV_QUERIES_HUMAN,
    PIXIV_USE_RANKING, PIXIV_RANKING_MODE, PIXIV_RANKING_DATE, PIXIV_LABEL,
    PIXIV_MIN_BOOKMARKS, PIXIV_MIN_VIEWS
)
from image_utils import process_and_save_image, HASH_CACHE, load_hash_cache_from_csv

router = Router[PlaywrightCrawlingContext]()

def get_save_path(url: str, label: str, prefix: str = "") -> Path:
    url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
    filename = f"{prefix}{label}_{url_hash}.webp"
    return IMAGES_DIR / label / filename

# ========================================================
# 1. REDDIT HANDLER (через JSON API)
# ========================================================
def reddit_unescape(url: str) -> str:
    """Reddit экранирует '&' как '&amp;' (на случай, если raw_json=1 не сработал)."""
    return (url or '').replace('&amp;', '&')


def preview_to_original(url: str) -> str | None:
    """preview.redd.it/<id>.<ext>?...&s=... → https://i.redd.it/<id>.<ext>
    Оригинал без подписи и в максимальном разрешении.
    Для external-preview.redd.it трюк не работает → None."""
    p = urlparse(url or '')
    if p.hostname == 'preview.redd.it' and p.path:
        return f"https://i.redd.it{p.path}"
    return None


async def _fetch_image_bytes(context: PlaywrightCrawlingContext, url: str) -> bytes | None:
    """Скачивает картинку из контекста браузера (куки + TLS-отпечаток).
    1) fetch() со страницы (CORS открыт для *.redd.it).
    2) Если fetch упал по CORS/сети (status 0) — навигация новой вкладкой с Referer."""
    result = await context.page.evaluate("""
        async (url) => {
            try {
                const resp = await fetch(url);
                if (!resp.ok) return { status: resp.status, data: null };
                const blob = await resp.blob();
                const b64 = await new Promise((resolve) => {
                    const reader = new FileReader();
                    reader.onloadend = () => resolve(reader.result.split(',')[1]);
                    reader.readAsDataURL(blob);
                });
                return { status: 200, data: b64 };
            } catch (e) {
                return { status: 0, data: null };
            }
        }
    """, url)

    if result and result.get('status') == 200 and result.get('data'):
        return base64.b64decode(result['data'])

    if not result or result.get('status') == 0:
        new_page = await context.page.context.new_page()
        try:
            response = await new_page.goto(
                url, referer='https://www.reddit.com/', wait_until='domcontentloaded'
            )
            if response and response.ok:
                return await response.body()
            context.log.warning(f"HTTP {response.status if response else 'unknown'} для {url[:60]}")
        finally:
            await new_page.close()
    else:
        context.log.warning(f"HTTP {result.get('status')} для {url[:60]}")
    return None


@router.handler(label='reddit')
async def handle_reddit(context: PlaywrightCrawlingContext) -> None:
    user_data = context.request.user_data
    subreddit = user_data.get('subreddit', '')
    label = user_data.get('image_label', 'ai')
    max_posts = user_data.get('max_posts', 50)

    collected = 0
    seen_urls: set[str] = set()
    context.log.info(f"🔥 Скраппинг r/{subreddit} через JSON API (Label: {label})")

    after = None
    while collected < max_posts:
        api_url = (
            f"https://www.reddit.com/r/{subreddit}/top.json?t=year&limit=100&raw_json=1"
            + (f"&after={after}" if after else "")
        )

        result = await context.page.evaluate("""
            async (url) => {
                try {
                    const resp = await fetch(url);
                    return { status: resp.status, text: await resp.text() };
                } catch (e) {
                    return { status: 0, text: String(e) };
                }
            }
        """, api_url)

        if result['status'] == 429:
            context.log.warning("⏳ Reddit rate limit, пауза 30 сек...")
            await context.page.wait_for_timeout(30_000)
            continue
        if result['status'] != 200:
            context.log.error(f"❌ Reddit API {result['status']}: {result['text'][:200]}")
            break

        try:
            data = json.loads(result['text'])
        except json.JSONDecodeError:
            context.log.error(f"❌ Не-JSON ответ Reddit: {result['text'][:200]}")
            break

        children = data.get('data', {}).get('children', [])
        after = data.get('data', {}).get('after')
        if not children:
            context.log.info("📭 Посты закончились.")
            break

        for child in children:
            if collected >= max_posts:
                break
            post = child.get('data', {})
            post_url = "https://www.reddit.com" + (post.get('permalink') or '')
            if post.get('is_video'):
                continue

            candidates: list[str] = []

            # 1) Галерея — все элементы сразу, без кликов
            if post.get('is_gallery'):
                for m in (post.get('media_metadata') or {}).values():
                    if not isinstance(m, dict) or m.get('status') != 'valid':
                        continue
                    if m.get('e') == 'Video':
                        continue
                    s = m.get('s') or {}
                    u = s.get('u') or s.get('gif')
                    if u:
                        candidates.append(reddit_unescape(u))

            # 2) Одиночное изображение: url = i.redd.it (оригинал)
            if not candidates:
                url = post.get('url') or ''
                if 'i.redd.it' in url:
                    candidates.append(url)

            # 3) Фолбэк: preview.source (внешние картинки)
            if not candidates:
                try:
                    candidates.append(reddit_unescape(post['preview']['images'][0]['source']['url']))
                except (KeyError, IndexError, TypeError):
                    pass

            for image_url in candidates:
                if collected >= max_posts:
                    break

                # Порядок попыток: i.redd.it-оригинал → исходный подписанный URL (не трогаем параметры!)
                attempts = []
                original = preview_to_original(image_url)
                if original:
                    attempts.append(original)
                attempts.append(image_url)

                if any(u in seen_urls for u in attempts):
                    continue
                seen_urls.update(attempts)

                save_path = get_save_path(attempts[0], label)
                if save_path.exists():
                    continue

                img_data, used_url = None, attempts[0]
                for cand in attempts:
                    img_data = await _fetch_image_bytes(context, cand)
                    if img_data:
                        used_url = cand
                        break
                if not img_data:
                    context.log.warning(f"❌ Не скачалось: {image_url[:70]}")
                    continue

                try:
                    metadata = process_and_save_image(img_data, used_url, label, save_path)
                    if metadata:
                        metadata.update({
                            "subreddit": subreddit,
                            "post_url": post_url,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "source": "reddit",
                        })
                        await context.push_data(metadata)
                        collected += 1
                        context.log.info(f"✅ {collected}/{max_posts} | {save_path.name}")
                except Exception as e:
                    context.log.warning(f"Ошибка обработки {used_url[:60]}: {e}")

        if not after:
            context.log.info("📭 Достигнут конец ленты Reddit.")
            break
        await context.page.wait_for_timeout(1500)
# ========================================================
# 2. CIVITAI HANDLER
# ========================================================
@router.handler(label='civitai')
async def handle_civitai(context: PlaywrightCrawlingContext) -> None:
    user_data = context.request.user_data
    limit = user_data.get('limit', 50)
    collected = 0
    seen_urls: set[str] = set()

    next_cursor = None
    context.log.info(f"🎯 Скраппинг Civitai через API (Лимит: {limit})")

    await context.page.wait_for_timeout(5000)

    while collected < limit:
        if next_cursor:
            api_url = f"https://civitai.com/api/v1/images?limit=100&cursor={next_cursor}&sort=Most%20Reactions&nsfw=false"
        else:
            api_url = f"https://civitai.com/api/v1/images?limit=100&sort=Most%20Reactions&nsfw=false"

        try:
            result = await context.page.evaluate("""
                async (url) => {
                    try {
                        const response = await fetch(url);
                        const text = await response.text();
                        return { status: response.status, text: text };
                    } catch (e) {
                        return { status: 0, text: e.message };
                    }
                }
            """, api_url)

            if result['status'] != 200:
                context.log.error(f"❌ Civitai API ошибка! Статус: {result['status']}. Ответ: {result['text'][:200]}")
                break

            data = json.loads(result['text'])
            items = data.get('items', [])
            metadata = data.get('metadata', {})
            next_cursor = metadata.get('nextCursor')

            if not items:
                context.log.info("Больше нет картинок на Civitai (конец API).")
                break

            context.log.info(f"📄 Загружено {len(items)} ссылок. Курсор: {str(next_cursor)[:20]}...")

            for item in items:
                if collected >= limit:
                    break

                img_url = item.get('url')
                if not img_url:
                    continue

                clean_url = img_url
                if 'width=' in clean_url:
                    clean_url = re.sub(r'width=\d+', 'width=1024', clean_url)

                if clean_url not in seen_urls:
                    seen_urls.add(clean_url)
                    save_path = get_save_path(clean_url, "ai", prefix="civitai_")

                    if save_path.exists():
                        continue

                    try:
                        response = await context.send_request(clean_url)
                        img_data = await response.read()

                        metadata = process_and_save_image(img_data, clean_url, "ai", save_path)

                        if metadata:
                            metadata.update({
                                "subreddit": "civitai",
                                "post_url": "",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "source": "civitai_api"
                            })
                            await context.push_data(metadata)
                            collected += 1
                            context.log.info(f"✅ {collected}/{limit} | {save_path.name}")
                        else:
                            if len(img_data) < 20000:
                                context.log.warning(f"⏭️ Пропуск (вес < 20КБ): {clean_url[:50]}")
                            elif img_data[:200].lstrip().startswith(b'<html') or img_data[:200].lstrip().startswith(b'<!DOCTYPE'):
                                context.log.warning(f"🚫 Пропуск (CDN вернул HTML): {clean_url[:50]}")

                    except Exception as e:
                        context.log.warning(f"Ошибка загрузки {clean_url[:50]}: {e}")

            if not next_cursor:
                context.log.info("Достигнут конец ленты (курсор пустой).")
                break

            await context.page.wait_for_timeout(1000)

        except Exception as e:
            context.log.warning(f"Не удалось обработать JSON Civitai: {e}")
            break

# ========================================================
# 3. TWITTER (X) HANDLER
# ========================================================
@router.handler(label='twitter')
async def handle_twitter(context: PlaywrightCrawlingContext) -> None:
    user_data = context.request.user_data
    tag = user_data.get('tag', 'art')
    limit = user_data.get('limit', 50)
    label = "human" 

    collected = 0
    seen_urls: set[str] = set()

    context.log.info(f"🐦 Скраппинг Twitter #{tag} (Cookie Auth)")

    if TWITTER_COOKIES and TWITTER_COOKIES[0]["value"] != "PASTE_YOUR_AUTH_TOKEN_HERE":
        cookies = []
        for c in TWITTER_COOKIES:
            cookie = dict(c)
            if cookie.get("domain") == ".twitter.com":
                cookie["domain"] = ".x.com"
            cookies.append(cookie)
        await context.page.context.add_cookies(cookies)
        context.log.info("✅ Куки внедрены. Навигация...")
        await context.page.goto(f"https://x.com/search?q=%23{tag}&src=typed_query&f=media", wait_until="domcontentloaded")
        await context.page.wait_for_timeout(5000)
    else:
        context.log.warning("⚠️ Куки Twitter не найдены в config.py! Вы упретесь в стену логина.")

    for scroll_attempt in range(20):
        if collected >= limit:
            break

        tweets = await context.page.evaluate("""
            () => {
                const items = [];
                const seen = new Set();
                document.querySelectorAll('img[src*="pbs.twimg.com/media"]').forEach(img => {
                    let src = img.src;
                    if (!src) return;
                    let cleanUrl = src.split('?')[0] + '?name=large&format=jpg';
                    if (!seen.has(cleanUrl)) {
                        seen.add(cleanUrl);
                        items.push({ url: cleanUrl });
                    }
                });
                return items;
            }
        """)

        new_posts = [p for p in tweets if p['url'] not in seen_urls]
        seen_urls.update([p['url'] for p in new_posts])

        for post in new_posts:
            if collected >= limit:
                break

            save_path = get_save_path(post['url'], label, prefix=f"twitter_{tag}_")
            if save_path.exists():
                continue

            try:
                response = await context.send_request(post['url'])
                if response.status_code != 200:
                    context.log.warning(f"HTTP {response.status_code} для {post['url'][:50]}")
                    continue
                metadata = process_and_save_image(
                    await response.read(), post['url'], label, save_path
                )

                if metadata:
                    metadata.update({
                        "subreddit": f"twitter_#{tag}",
                        "post_url": "",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "source": "twitter_cookie"
                    })
                    await context.push_data(metadata)
                    collected += 1
                    context.log.info(f"✅ {collected}/{limit} | {save_path.name}")
            except Exception as e:
                context.log.warning(f"Ошибка загрузки {post['url'][:50]}: {e}")

        await context.page.evaluate("window.scrollBy(0, 1000)")
        await context.page.wait_for_timeout(3000)

# ========================================================
# 4. ARTSTATION HANDLER
# ========================================================
@router.handler(label='artstation')
async def handle_artstation(context: PlaywrightCrawlingContext) -> None:
    user_data = context.request.user_data
    query = user_data.get('query', 'art')
    limit = user_data.get('limit', 50)
    label = user_data.get('label', 'human')

    collected = 0
    seen_urls: set[str] = set()
    context.log.info(f"🎨 Скраппинг ArtStation '{query}' через API (Label: {label})")

    try:
        json_text = await context.page.evaluate("() => document.body.innerText")
        data = json.loads(json_text)
    except Exception as e:
        context.log.warning(f"Не удалось прочитать JSON ArtStation: {e}")
        return

    projects = data.get('projects', [])
    context.log.info(f"Найдено {len(projects)} проектов на первой странице.")

    for project in projects:
        if collected >= limit:
            break

        img_url = project.get('cover', {}).get('image_url')
        if img_url:
            clean_url = img_url.replace('/small/', '/large/').replace('/micro/', '/large/').replace('/thumb/', '/large/')
            clean_url = clean_url.split('?')[0]

            if clean_url not in seen_urls:
                seen_urls.add(clean_url)
                save_path = get_save_path(clean_url, label, prefix="artstation_")

                if save_path.exists():
                    continue

                try:
                    img_response = await context.send_request(clean_url)
                    if img_response.status_code != 200:
                        context.log.warning(f"HTTP {img_response.status_code} для {clean_url[:50]}")
                        continue
                    metadata = process_and_save_image(
                        await img_response.read(), clean_url, label, save_path
                    )

                    if metadata:
                        metadata.update({
                            "subreddit": f"artstation_{query}",
                            "post_url": project.get('permalink', ''),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "source": "artstation_api"
                        })
                        await context.push_data(metadata)
                        collected += 1
                        context.log.info(f"✅ {collected}/{limit} | {save_path.name}")
                except Exception as e:
                    context.log.warning(f"Ошибка загрузки {clean_url[:50]}: {e}")

# ========================================================
# PIXIV HANDLER (Ранкинг + Поиск + Фильтр по популярности)
# ========================================================
@router.handler(label='pixiv')
async def handle_pixiv(context: PlaywrightCrawlingContext) -> None:
    """
    Автоопределение режима:
    - user_data['mode']  → РАНКИНГ (illust_ranking)
    - user_data['query'] → ПОИСК (search_illust)
    - Ничего не указано  → берётся из config.py
    
    Настройки по умолчанию читаются из config.py,
    но могут быть переопределены через user_data.
    """
    user_data = context.request.user_data
    
    # --- 1. ОПРЕДЕЛЕНИЕ РЕЖИМА (config → user_data) ---
    has_mode = user_data.get('mode') not in (None, '')
    has_query = user_data.get('query') not in (None, '')
    
    if has_mode:
        is_ranking = True
    elif has_query:
        is_ranking = False
    else:
        # Дефолт из config.py
        is_ranking = PIXIV_USE_RANKING
    
    # --- 2. ПАРАМЕТРЫ (config.py → user_data override) ---
    if is_ranking:
        mode = user_data.get('mode', PIXIV_RANKING_MODE)
        query = None
    else:
        mode = None
        query = user_data.get('query', PIXIV_QUERIES_HUMAN[0] if PIXIV_QUERIES_HUMAN else 'digital art')
    
    date = user_data.get('date', PIXIV_RANKING_DATE if PIXIV_RANKING_DATE else None)
    limit = user_data.get('limit', PIXIV_LIMIT)
    label = user_data.get('label', PIXIV_LABEL)
    min_bookmarks = user_data.get('min_bookmarks', PIXIV_MIN_BOOKMARKS)
    min_views = user_data.get('min_views', PIXIV_MIN_VIEWS)
    
    # --- 3. ЛОГИ ---
    mode_str = f"mode={mode}" if is_ranking else f"query='{query}'"
    context.log.info(f"🎌 Pixiv | {'🏆 Ranking' if is_ranking else '🔍 Search'} | {mode_str}")
    context.log.info(f"📊 Параметры: limit={limit} | label={label} | bookmarks≥{min_bookmarks} | views≥{min_views}")
    
    collected = 0
    seen_urls: set[str] = set()
    skipped_low_stats = 0
    skipped_manga = 0
    
    # --- 4. AUTH ---
    def init_api():
        api = AppPixivAPI()
        if PIXIV_PROXY:
            api.requests_kwargs = {
                'proxies': {'http': PIXIV_PROXY, 'https': PIXIV_PROXY},
                'verify': True,
            }
        try:
            api.auth(refresh_token=PIXIV_REFRESH_TOKEN)
            return api
        except Exception as e:
            context.log.error(f"❌ Pixiv auth FAILED: {e}")
            return None
    
    api = await asyncio.to_thread(init_api)
    if api is None:
        return
    
    # --- 5. СЕССИЯ ДЛЯ СКАЧИВАНИЯ ---
    connector = aiohttp.TCPConnector(ssl=False, limit=10)
    timeout = aiohttp.ClientTimeout(total=30)
    proxy = PIXIV_PROXY if PIXIV_PROXY else None
    
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as download_session:
        next_qs = None
        
        while collected < limit:
            try:
                # --- 6. ВЫБОР API-МЕТОДА ---
                def fetch_page():
                    nonlocal next_qs
                    
                    if is_ranking:
                        # === РАНКИНГ ===
                        if next_qs is None:
                            kwargs = {"mode": mode}
                            if date:
                                kwargs["date"] = date
                            return api.illust_ranking(**kwargs)
                        else:
                            return api.illust_ranking(**next_qs)
                    else:
                        # === ПОИСК ===
                        if next_qs is None:
                            kwargs = {
                                "word": query,
                                "search_target": 'partial_match_for_tags',
                                "sort": 'popular_desc',
                                "filter": 'for_ios'
                            }
                            return api.search_illust(**kwargs)
                        else:
                            return api.search_illust(**next_qs)
                
                json_result = await asyncio.to_thread(fetch_page)
                
                if not hasattr(json_result, 'illusts') or not json_result.illusts:
                    context.log.info("📭 Pixiv: больше нет иллюстраций.")
                    break
                
                context.log.info(f"📄 Получено {len(json_result.illusts)} работ")
                
                # --- 7. ОБРАБОТКА РАБОТ ---
                for illust in json_result.illusts:
                    if collected >= limit:
                        break
                    
                    # Пропускаем мангу и анимацию
                    if illust.page_count > 1 or illust.type != 'illust':
                        skipped_manga += 1
                        continue
                    
                    # --- ФИЛЬТР ПО ПОПУЛЯРНОСТИ ---
                    bookmarks = getattr(illust, 'total_bookmarks', 0) or 0
                    views = getattr(illust, 'total_view', 0) or 0
                    
                    if min_bookmarks > 0 and bookmarks < min_bookmarks:
                        skipped_low_stats += 1
                        context.log.info(
                            f"⏭️ Пропуск (закладок {bookmarks} < {min_bookmarks}): "
                            f"{getattr(illust, 'title', '???')[:40]}"
                        )
                        continue
                    
                    if min_views > 0 and views < min_views:
                        skipped_low_stats += 1
                        context.log.info(
                            f"⏭️ Пропуск (просмотров {views} < {min_views}): "
                            f"{getattr(illust, 'title', '???')[:40]}"
                        )
                        continue
                    
                    # --- URL ИЗОБРАЖЕНИЯ ---
                    original_url = illust.meta_single_page.get('original_image_url')
                    large_url = illust.image_urls.get('large')
                    medium_url = illust.image_urls.get('medium')
                    
                    target_url = original_url or large_url or medium_url
                    if not target_url:
                        continue
                    
                    if target_url in seen_urls:
                        continue
                    seen_urls.add(target_url)
                    
                    save_path = get_save_path(target_url, label, prefix="pixiv_")
                    if save_path.exists():
                        continue
                    
                    # --- 8. СКАЧИВАНИЕ С FALLBACK ---
                    try:
                        headers = {
                            "Referer": "https://www.pixiv.net/",
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0"
                        }
                        
                        async with download_session.get(
                            target_url, headers=headers, proxy=proxy
                        ) as resp:
                            
                            if resp.status != 200:
                                fallback = large_url if target_url == original_url else medium_url
                                if fallback and fallback != target_url:
                                    context.log.warning(
                                        f"⏭️ Pixiv {resp.status} на original, пробуем fallback"
                                    )
                                    async with download_session.get(
                                        fallback, headers=headers, proxy=proxy
                                    ) as resp2:
                                        if resp2.status != 200:
                                            context.log.warning(f"⏭️ Pixiv {resp2.status} пропущено")
                                            continue
                                        img_data = await resp2.read()
                                        target_url = fallback
                                else:
                                    context.log.warning(
                                        f"⏭️ Pixiv {resp.status} пропущено: {target_url[:60]}"
                                    )
                                    continue
                            else:
                                img_data = await resp.read()
                        
                        # --- 9. СОХРАНЕНИЕ ---
                        metadata = process_and_save_image(img_data, target_url, label, save_path)
                        
                        if metadata:
                            author_name = ""
                            if hasattr(illust, 'user') and illust.user:
                                author_name = illust.user.get('name', '')
                            
                            source_type = "pixiv_ranking" if is_ranking else "pixiv_search"
                            tag_name = mode if is_ranking else query
                            
                            metadata.update({
                                "subreddit": f"pixiv_{tag_name}",
                                "post_url": f"https://www.pixiv.net/artworks/{illust.id}",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "source": source_type,
                                "bookmarks": bookmarks,
                                "views": views,
                                "likes": bookmarks,
                                "title": getattr(illust, 'title', ''),
                                "author": author_name,
                            })
                            await context.push_data(metadata)
                            collected += 1
                            context.log.info(
                                f"✅ {collected}/{limit} | "
                                f"💖{bookmarks} | 👁{views} | "
                                f"{save_path.name}"
                            )
                        else:
                            context.log.warning(
                                f"⏭️ process_and_save_image вернул None: {target_url[:60]}"
                            )
                            
                    except Exception as e:
                        context.log.warning(f"⚠️ Ошибка скачивания {target_url[:50]}: {e}")
                
                # --- 10. ПАГИНАЦИЯ ---
                next_qs = api.parse_qs(json_result.next_url)
                if not next_qs:
                    context.log.info("📭 Достигнут конец ленты.")
                    break
                await context.page.wait_for_timeout(1500)
                
            except Exception as e:
                context.log.error(f"❌ Pixiv критическая ошибка: {e}")
                break
    
    context.log.info(
        f"🎌 Pixiv завершён: {collected} иллюстраций | "
        f"пропущено по фильтру: {skipped_low_stats} | "
        f"пропущено манга/анимация: {skipped_manga}"
    )

# ========================================================
# 6. MAIN EXECUTION
# ========================================================
async def main():
    load_hash_cache_from_csv(Path(METADATA_FILE))

    requests = []

    for sub in SUBREDDITS_AI:
        requests.append(Request.from_url(
            url=f"https://www.reddit.com/r/{sub}/top/?t=month", 
            label="reddit", 
            user_data={"subreddit": sub, "image_label": "ai", "max_posts": MAX_POSTS_PER_SUB}
        ))
    for sub in SUBREDDITS_HUMAN:
        requests.append(Request.from_url(
            url=f"https://www.reddit.com/r/{sub}/top/?t=month", 
            label="reddit", 
            user_data={"subreddit": sub, "image_label": "human", "max_posts": MAX_POSTS_PER_SUB}
        ))

    requests.append(Request.from_url(
        url="https://civitai.com/images?sort=highest%20rated", 
        label="civitai", 
        user_data={"limit": CIVITAI_LIMIT}
    ))

    for tag in TWITTER_TAGS:
        requests.append(Request.from_url(
            url=f"https://x.com/search?q=%23{tag}&src=typed_query&f=media", 
            label="twitter", 
            user_data={"tag": tag, "limit": TWITTER_LIMIT}
        ))

    for query in ARTSTATION_QUERIES_HUMAN:
        q_url = query.replace(" ", "%20")
        requests.append(Request.from_url(
            url=f"https://www.artstation.com/api/v2/search/projects.json?q={q_url}&page=1&sort_by=likes",
            label="artstation", 
            user_data={"query": query, "limit": ARTSTATION_LIMIT, "label": "human"}
        ))

    for query in ARTSTATION_QUERIES_AI:
        q_url = query.replace(" ", "%20")
        requests.append(Request.from_url(
            url=f"https://www.artstation.com/api/v2/search/projects.json?q={q_url}&page=1&sort_by=likes",
            label="artstation", 
            user_data={"query": query, "limit": ARTSTATION_LIMIT, "label": "ai"}
        ))
    
    # 7. Pixiv Ranking
    if PIXIV_USE_RANKING:
        requests.append(Request.from_url(
            url="https://www.pixiv.net/",
            label="pixiv",
            user_data={
                "mode": PIXIV_RANKING_MODE,
                "date": PIXIV_RANKING_DATE if PIXIV_RANKING_DATE else None,
                "limit": PIXIV_LIMIT,
                "label": PIXIV_LABEL
            }
        ))
    else:
        for query in PIXIV_QUERIES_HUMAN:
            requests.append(Request.from_url(
                url="https://www.pixiv.net/",
                label="pixiv",
                user_data={"query": query, "limit": PIXIV_LIMIT // len(PIXIV_QUERIES_HUMAN), "label": "human"}
            ))

    crawler = PlaywrightCrawler(
        request_handler=router,
        headless=False, 
        request_handler_timeout=timedelta(minutes=20)
    )

    await crawler.run(requests)

    print(f"\n📁 Экспорт метаданных в {METADATA_FILE}...")
    dataset = await Dataset.open()
    data = await dataset.get_data()

    if data.items:
        file_exists = Path(METADATA_FILE).exists()
        keys = data.items[0].keys()
        with open(METADATA_FILE, 'a' if file_exists else 'w', newline='', encoding='utf-8') as output_file:
            dict_writer = csv.DictWriter(output_file, fieldnames=keys)
            if not file_exists:
                dict_writer.writeheader()
            dict_writer.writerows(data.items)
        print(f"✅ Скраппинг завершен! Данные добавлены в {METADATA_FILE}")
    else:
        print("⚠️ Новых данных не найдено.")

    print(f"📊 Всего уникальных изображений в кэше: {len(HASH_CACHE)}")

if __name__ == "__main__":
    print("🚀 Запуск скраппера (Режим: ТОП работы)...")
    asyncio.run(main())