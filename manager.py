"""
Stock News Ticker Plugin for LEDMatrix

Displays scrolling stock-specific news headlines and financial updates from RSS feeds.
Shows market news, company announcements, and financial updates for tracked stocks.

Features:
- Stock-specific RSS feeds and news aggregation
- Symbol tracking and filtering
- Scrolling headline display
- Custom RSS feed support
- Configurable scroll speed and colors
- Background data fetching

API Version: 1.0.0
"""

import time
import requests
import xml.etree.ElementTree as ET
import html
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

from src.logging_config import get_logger
from src.plugin_system.base_plugin import BasePlugin
from src.common.scroll_helper import ScrollHelper


class StockNewsTickerPlugin(BasePlugin):
    """
    Stock news ticker plugin for displaying financial headlines.

    Tracks specific stock symbols and displays relevant news headlines
    from financial RSS feeds with configurable display options.

    Configuration options:
        feeds: Stock symbols to track and custom RSS feeds
        display_options: Scroll speed, duration, colors
        background_service: Data fetching configuration
    """

    # Type coercion helpers -------------------------------------------------

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        """Coerce a config value to int, falling back to *default* on error."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        """Coerce a config value to float, falling back to *default* on error."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_color(value: Any, default: Tuple[int, ...]) -> Tuple[int, ...]:
        """Coerce a config color value to an int tuple, falling back to *default*."""
        try:
            result = tuple(int(float(c)) for c in value)
            if len(result) == 3:
                return result
        except (TypeError, ValueError, AttributeError):
            pass
        return default

    # ------------------------------------------------------------------

    def __init__(self, plugin_id: str, config: Dict[str, Any],
                 display_manager, cache_manager, plugin_manager) -> None:
        """Initialize the stock news ticker plugin."""
        super().__init__(plugin_id, config, display_manager, cache_manager, plugin_manager)

        # Tell the DisplayController this plugin needs the high-FPS scroll
        # loop instead of the default 1 Hz tick. Without this flag,
        # display() is only called once per second, which makes 30 px/s
        # scrolling jump 30 pixels per frame (visible as severe judder).
        # ScrollHelper alone isn't enough — the framework gates high-FPS
        # purely on this attribute being True.
        self.enable_scrolling = True

        # Configuration
        self.feeds_config = config.get('feeds', {})
        self.global_config = config.get('global', {})

        # Display settings (type-coerced)
        self.display_duration = self._to_float(self.global_config.get('display_duration', 30), 30)
        self.scroll_speed = self._to_float(self.global_config.get('scroll_speed', 1), 1)
        self.scroll_delay = self._to_float(self.global_config.get('scroll_delay', 0.01), 0.01)
        self.dynamic_duration = bool(self.global_config.get('dynamic_duration', True))
        self.min_duration = self._to_float(self.global_config.get('min_duration', 30), 30)
        self.max_duration = self._to_float(self.global_config.get('max_duration', 300), 300)
        self.max_headlines_per_symbol = self._to_int(
            self.global_config.get('max_headlines_per_symbol', 1), 1)
        self.headlines_per_rotation = self._to_int(
            self.global_config.get('headlines_per_rotation', 2), 2)
        self.font_name = self.global_config.get('font', '10x20')
        self.news_source = self.feeds_config.get('news_source', 'google_news')
        self.market_feeds = self.feeds_config.get('market_feeds', {})

        # Colors (type-coerced)
        self.text_color = self._to_color(
            self.feeds_config.get('text_color', [0, 255, 0]), (0, 255, 0))
        self.symbol_color = self._to_color(
            self.feeds_config.get('symbol_color', [255, 255, 0]), (255, 255, 0))
        self.separator_color = self._to_color(
            self.feeds_config.get('separator_color', [255, 0, 0]), (255, 0, 0))

        # Background service configuration
        self.background_config = self.global_config.get('background_service', {
            'enabled': True,
            'request_timeout': 30,
            'max_retries': 5,
            'priority': 2
        })

        # State
        self.current_news_items: List[Dict[str, Any]] = []
        self.last_update: float = 0
        self.all_news_items: List[Dict[str, Any]] = []
        self.initialized: bool = True
        self._scrolling_dirty: bool = True  # Rebuild scrolling image on next display()

        # Derive font_size from the BDF font map (used for text layout)
        font_entry = self._BDF_FONT_MAP.get(self.font_name, ('10x20.bdf', 20))
        self.font_size = font_entry[1]

        # Cached fonts (loaded once, reused in display loop)
        self._headline_font: Optional[ImageFont.ImageFont] = None
        self._info_font: Optional[ImageFont.ImageFont] = None
        self._load_fonts()

        # Initialize ScrollHelper for proper horizontal scrolling
        matrix_width = self.display_manager.matrix.width
        matrix_height = self.display_manager.matrix.height
        self.scroll_helper = ScrollHelper(matrix_width, matrix_height, self.logger)
        pixels_per_second = (
            self.scroll_speed / self.scroll_delay
            if self.scroll_delay > 0 else self.scroll_speed * 100
        )
        self.scroll_helper.set_scroll_speed(pixels_per_second)
        self.scroll_helper.set_scroll_delay(self.scroll_delay)

        # Register fonts with font manager
        self._register_fonts()

        # Log configuration
        stock_symbols = self.feeds_config.get('stock_symbols', [])
        custom_feeds = list(self.feeds_config.get('custom_feeds', {}).keys())

        self.logger.info("Stock news ticker plugin initialized")
        self.logger.info("Tracking symbols: %s", stock_symbols)
        self.logger.info("Custom feeds: %s", custom_feeds)

    # BDF font name → (filename, native pixel size).
    # BDF fonts are fixed-size bitmaps — they must be loaded at their native size.
    _BDF_FONT_MAP: Dict[str, tuple] = {
        '10x20':  ('10x20.bdf',  20),
        '9x18B':  ('9x18B.bdf',  18),
        '9x18':   ('9x18.bdf',   18),
        '9x15B':  ('9x15B.bdf',  15),
        '9x15':   ('9x15.bdf',   15),
        '8x13B':  ('8x13B.bdf',  13),
        '8x13':   ('8x13.bdf',   13),
        '7x14B':  ('7x14B.bdf',  14),
        '7x14':   ('7x14.bdf',   14),
        '7x13B':  ('7x13B.bdf',  13),
        '7x13':   ('7x13.bdf',   13),
        '6x13B':  ('6x13B.bdf',  13),
        '6x12':   ('6x12.bdf',   12),
        '6x10':   ('6x10.bdf',   10),
    }

    # Built-in RSS URL templates keyed by news_source config value.
    _NEWS_SOURCE_URLS: Dict[str, str] = {
        'yahoo': 'https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US',
        'google_news': 'https://news.google.com/rss/search?q={symbol}+stock&hl=en-US&gl=US&ceid=US:en',
        'seeking_alpha': 'https://seekingalpha.com/api/sa/combined/{symbol}.xml',
    }

    # Built-in general market feed URLs.
    _MARKET_FEED_URLS: Dict[str, str] = {
        'cnbc': 'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114',
        'marketwatch': 'https://feeds.marketwatch.com/marketwatch/topstories/',
    }

    def _load_fonts(self) -> None:
        """Load and cache PIL fonts for display rendering.

        Only TTF/OTF fonts can be loaded via ImageFont.truetype().
        BDF bitmap fonts require freetype and are NOT compatible with
        this fallback chain.

        Paths are resolved relative to the project root (two levels
        above the installed plugin directory) so fonts load regardless
        of the process working directory.
        """
        # Resolve project root: plugins/<id>/manager.py → project root
        project_root = Path(__file__).resolve().parent.parent.parent
        fonts_dir = project_root / 'assets' / 'fonts'

        # Headline / symbol font — try TTF fonts, then PIL default
        for font_name in ('PressStart2P-Regular.ttf', '4x6-font.ttf'):
            font_path = fonts_dir / font_name
            try:
                self._headline_font = ImageFont.truetype(str(font_path), self.font_size)
                self.logger.debug("Loaded headline font: %s", font_path)
                break
            except (OSError, IOError):
                continue

        if self._headline_font is None:
            self._headline_font = ImageFont.load_default()
            self.logger.warning("Using PIL default font for headlines")

        # Info font (smaller, for source / timestamp)
        try:
            self._info_font = ImageFont.truetype(str(fonts_dir / '4x6-font.ttf'), 8)
        except (OSError, IOError):
            self._info_font = ImageFont.load_default()
            self.logger.warning("Using PIL default font for info text")

    def _register_fonts(self) -> None:
        """Register fonts with the font manager."""
        try:
            if not hasattr(self.plugin_manager, 'font_manager'):
                return

            font_manager = self.plugin_manager.font_manager
            font_entry = self._BDF_FONT_MAP.get(self.font_name, ('10x20.bdf', 20))
            family = font_entry[0].replace('.bdf', '')
            size_px = font_entry[1]

            # Headline font
            font_manager.register_manager_font(
                manager_id=self.plugin_id,
                element_key=f"{self.plugin_id}.headline",
                family=family,
                size_px=size_px,
                color=self.text_color
            )

            # Symbol font
            font_manager.register_manager_font(
                manager_id=self.plugin_id,
                element_key=f"{self.plugin_id}.symbol",
                family=family,
                size_px=size_px,
                color=self.symbol_color
            )

            # Separator font
            font_manager.register_manager_font(
                manager_id=self.plugin_id,
                element_key=f"{self.plugin_id}.separator",
                family=family,
                size_px=size_px,
                color=self.separator_color
            )

            # Info font (source, time)
            font_manager.register_manager_font(
                manager_id=self.plugin_id,
                element_key=f"{self.plugin_id}.info",
                family="6x10",
                size_px=10,
                color=(150, 150, 150)
            )

            self.logger.info("Stock news ticker fonts registered")
        except (AttributeError, OSError, ValueError) as e:
            self.logger.warning("Error registering fonts: %s", e)

    def update(self) -> None:
        """Update stock news headlines for all tracked symbols."""
        if not self.initialized:
            return

        try:
            self.current_news_items = []
            self.all_news_items = []

            # Get stock symbols to track
            stock_symbols = self.feeds_config.get('stock_symbols', [])

            # Fetch news for each symbol
            for symbol in stock_symbols:
                symbol_news = self._fetch_stock_news(symbol)
                if symbol_news:
                    self.all_news_items.extend(symbol_news)

            # Fetch from built-in market feeds (CNBC, MarketWatch)
            market_news = self._fetch_market_feeds()
            if market_news:
                self.all_news_items.extend(market_news)

            # Fetch from custom feeds
            custom_feeds = self.feeds_config.get('custom_feeds', {})
            for feed_name, feed_url in custom_feeds.items():
                custom_news = self._fetch_feed_headlines(feed_name, feed_url)
                if custom_news:
                    self.all_news_items.extend(custom_news)

            # Limit total news items and reset rotation tracking
            max_items = len(stock_symbols) * self.max_headlines_per_symbol + len(custom_feeds) * self.headlines_per_rotation
            if len(self.all_news_items) > max_items:
                self.all_news_items = self.all_news_items[:max_items]

            # Reset rotation tracking for new content
            if self.all_news_items:
                self.current_rotation_index = 0

            self._scrolling_dirty = True  # Rebuild scrolling image on next display()
            self.last_update = time.time()
            self.logger.debug("Updated stock news: %d total items", len(self.all_news_items))

        except (requests.RequestException, KeyError, TypeError, ValueError) as e:
            self.logger.error("Error updating stock news: %s", e)

    def _fetch_stock_news(self, symbol: str) -> List[Dict]:
        """Fetch news for a stock symbol from the configured RSS source.

        Routes to the appropriate RSS provider based on ``self.news_source``.
        Results are cached per (source, symbol) to avoid redundant requests.

        Args:
            symbol: Ticker symbol (e.g. ``'NVDA'``).

        Returns:
            List of news-item dicts with ``symbol``, ``title``, ``source``, etc.
        """
        source = self.news_source
        cache_key = f"stock_news_{source}_{symbol}_{datetime.now().strftime('%Y%m%d%H')}"
        update_interval = self.global_config.get('update_interval', 300)

        # Check cache first
        cached_data = self.cache_manager.get(cache_key)
        if cached_data and (time.time() - self.last_update) < update_interval:
            self.logger.debug("Using cached %s news for %s", source, symbol)
            return cached_data

        url_template = self._NEWS_SOURCE_URLS.get(source)
        if not url_template:
            self.logger.warning("Unknown news source '%s', falling back to google_news", source)
            url_template = self._NEWS_SOURCE_URLS['google_news']
            source = 'google_news'

        feed_url = url_template.format(symbol=symbol)
        source_label = {'yahoo': 'Yahoo Finance', 'google_news': 'Google News',
                        'seeking_alpha': 'Seeking Alpha'}.get(source, source)

        try:
            self.logger.info("Fetching %s news for %s...", source_label, symbol)
            headers = {'User-Agent': 'LEDMatrix-StockNewsPlugin/1.0 (RSS Reader)'}
            response = requests.get(
                feed_url,
                timeout=self.background_config.get('request_timeout', 30),
                headers=headers,
            )
            response.raise_for_status()

            root = ET.fromstring(response.content)
            news_items = []

            for item in root.findall('.//item')[:self.max_headlines_per_symbol]:
                title = item.find('title')
                description = item.find('description')
                pub_date = item.find('pubDate')
                link = item.find('link')

                if title is not None and title.text:
                    news_items.append({
                        'symbol': symbol,
                        'title': self._clean_headline(html.unescape(title.text).strip()),
                        'summary': (html.unescape(description.text).strip()
                                    if description is not None and description.text else ''),
                        'source': source_label,
                        'published': pub_date.text if pub_date is not None else '',
                        'url': link.text if link is not None else '',
                    })

            self.cache_manager.set(cache_key, news_items, ttl=update_interval * 2)
            self.logger.debug("Fetched %d %s items for %s", len(news_items), source_label, symbol)
            return news_items

        except requests.RequestException as e:
            self.logger.error("Error fetching %s news for %s: %s", source_label, symbol, e)
        except ET.ParseError as e:
            self.logger.error("Error parsing %s RSS for %s: %s", source_label, symbol, e)
        except Exception as e:
            self.logger.error("Error processing %s news for %s: %s", source_label, symbol, e)

        return []

    def _fetch_market_feeds(self) -> List[Dict]:
        """Fetch headlines from enabled built-in market feeds (CNBC, MarketWatch).

        Returns:
            List of news-item dicts from all enabled market feeds.
        """
        all_items: List[Dict] = []
        for feed_key, feed_url in self._MARKET_FEED_URLS.items():
            if not self.market_feeds.get(feed_key, False):
                continue

            feed_label = {'cnbc': 'CNBC', 'marketwatch': 'MarketWatch'}.get(feed_key, feed_key)
            cache_key = f"market_feed_{feed_key}_{datetime.now().strftime('%Y%m%d%H')}"
            update_interval = self.global_config.get('update_interval', 300)

            cached = self.cache_manager.get(cache_key)
            if cached and (time.time() - self.last_update) < update_interval:
                all_items.extend(cached)
                continue

            try:
                self.logger.info("Fetching %s market news...", feed_label)
                headers = {'User-Agent': 'LEDMatrix-StockNewsPlugin/1.0 (RSS Reader)'}
                response = requests.get(
                    feed_url,
                    timeout=self.background_config.get('request_timeout', 30),
                    headers=headers,
                )
                response.raise_for_status()

                root = ET.fromstring(response.content)
                items: List[Dict] = []

                for item in root.findall('.//item')[:self.headlines_per_rotation]:
                    title = item.find('title')
                    pub_date = item.find('pubDate')
                    link = item.find('link')

                    if title is not None and title.text:
                        items.append({
                            'feed_name': feed_label,
                            'title': self._clean_headline(html.unescape(title.text).strip()),
                            'source': feed_label,
                            'published': pub_date.text if pub_date is not None else '',
                            'url': link.text if link is not None else '',
                        })

                self.cache_manager.set(cache_key, items, ttl=update_interval * 2)
                self.logger.debug("Fetched %d headlines from %s", len(items), feed_label)
                all_items.extend(items)

            except requests.RequestException as e:
                self.logger.error("Error fetching %s feed: %s", feed_label, e)
            except ET.ParseError as e:
                self.logger.error("Error parsing %s feed: %s", feed_label, e)
            except Exception as e:
                self.logger.error("Error processing %s feed: %s", feed_label, e)

        return all_items

    def _fetch_feed_headlines(self, feed_name: str, feed_url: str) -> List[Dict]:
        """Fetch headlines from a custom RSS feed."""
        cache_key = f"stock_feed_{feed_name}_{datetime.now().strftime('%Y%m%d%H')}"
        update_interval = self.global_config.get('update_interval', 300)

        # Check cache first
        cached_data = self.cache_manager.get(cache_key)
        if cached_data and (time.time() - self.last_update) < update_interval:
            self.logger.debug(f"Using cached headlines for {feed_name}")
            return cached_data

        try:
            self.logger.info(f"Fetching stock headlines from {feed_name}...")
            response = requests.get(feed_url, timeout=self.background_config.get('request_timeout', 30))
            response.raise_for_status()

            # Parse RSS XML
            root = ET.fromstring(response.content)
            headlines = []

            # Extract headlines from RSS items
            for item in root.findall('.//item')[:self.headlines_per_rotation]:
                title = item.find('title')
                description = item.find('description')
                pub_date = item.find('pubDate')
                link = item.find('link')

                if title is not None and title.text:
                    headline = {
                        'feed_name': feed_name,
                        'title': html.unescape(title.text).strip(),
                        'description': html.unescape(description.text).strip() if description is not None else '',
                        'published': pub_date.text if pub_date is not None else '',
                        'link': link.text if link is not None else '',
                        'timestamp': datetime.now().isoformat()
                    }

                    # Clean up the title
                    headline['title'] = self._clean_headline(headline['title'])
                    headlines.append(headline)

            # Cache the results
            self.cache_manager.set(cache_key, headlines, ttl=update_interval * 2)

            return headlines

        except requests.RequestException as e:
            self.logger.error(f"Error fetching RSS feed {feed_name}: {e}")
            return []
        except ET.ParseError as e:
            self.logger.error(f"Error parsing RSS feed {feed_name}: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Error processing RSS feed {feed_name}: {e}")
            return []

    def _clean_headline(self, headline: str) -> str:
        """Clean and format headline text."""
        if not headline:
            return ""

        # Remove extra whitespace
        headline = re.sub(r'\s+', ' ', headline.strip())

        # Remove common artifacts
        headline = re.sub(r'^\s*-\s*', '', headline)  # Remove leading dashes
        headline = re.sub(r'\s+', ' ', headline)  # Normalize whitespace

        # Limit length for display
        if len(headline) > 80:
            headline = headline[:77] + "..."

        return headline

    def display(self, force_clear: bool = False) -> None:
        """Display scrolling stock news headlines.

        Builds a wide PIL image with all headlines laid out horizontally,
        then uses ScrollHelper to smoothly scroll a visible window across it.
        """
        if not self.initialized:
            self._display_error("Stock news ticker plugin not initialized")
            return

        if not self.all_news_items:
            self._display_no_news()
            return

        # Rebuild the wide scrolling image when data changes
        if self._scrolling_dirty or self.scroll_helper.cached_image is None:
            self._build_scrolling_image()
            self._scrolling_dirty = False

        # Advance scroll position and render visible portion
        self.scroll_helper.update_scroll_position()
        visible = self.scroll_helper.get_visible_portion()

        if visible is not None:
            self.display_manager.image = visible
            self.display_manager.update_display()

    def _build_scrolling_image(self) -> None:
        """Build a wide PIL image with all headlines arranged horizontally.

        Each headline is rendered as a panel: [SYMBOL: headline text]
        using the cached fonts. ScrollHelper handles leading/trailing
        padding automatically via create_scrolling_image().
        """
        matrix_height = self.display_manager.matrix.height
        panels: List[Image.Image] = []

        for news_item in self.all_news_items:
            symbol = news_item.get('symbol', news_item.get('feed_name', ''))
            title = news_item.get('title', 'No title')

            # Measure text to determine panel width
            label = f"{symbol}: " if symbol else ""
            full_text = f"{label}{title}"

            # Use a temp image to measure text width
            tmp = Image.new('RGB', (1, 1))
            tmp_draw = ImageDraw.Draw(tmp)
            bbox = tmp_draw.textbbox((0, 0), full_text, font=self._headline_font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]

            # Create panel with padding
            panel_width = text_width + 16  # 8px padding each side
            panel = Image.new('RGB', (panel_width, matrix_height), (0, 0, 0))
            draw = ImageDraw.Draw(panel)

            # Center text vertically
            y = max(0, (matrix_height - text_height) // 2)

            # Draw symbol in symbol_color, then headline in text_color
            if label:
                draw.text((8, y), label, fill=self.symbol_color, font=self._headline_font)
                label_bbox = draw.textbbox((8, y), label, font=self._headline_font)
                title_x = label_bbox[2]
                draw.text((title_x, y), title, fill=self.text_color, font=self._headline_font)
            else:
                draw.text((8, y), title, fill=self.text_color, font=self._headline_font)

            # Thin separator bar on the right edge
            sep_x = panel_width - 2
            draw.line([(sep_x, 4), (sep_x, matrix_height - 4)],
                      fill=self.separator_color, width=1)

            panels.append(panel)

        if panels:
            self.scroll_helper.create_scrolling_image(panels, item_gap=24, element_gap=0)
            self.logger.info("Built news scrolling image: %d panels, %dpx total",
                             len(panels), self.scroll_helper.total_scroll_width)
        else:
            self.scroll_helper.create_scrolling_image([], item_gap=0, element_gap=0)

    def is_cycle_complete(self) -> bool:
        """Check if the scroll has completed one full cycle."""
        return self.scroll_helper.is_scroll_complete()

    def reset_cycle_state(self) -> None:
        """Reset scroll to the beginning for the next rotation."""
        self.scroll_helper.reset_scroll()

    def _display_no_news(self):
        """Display message when no news is available."""
        img = Image.new('RGB', (self.display_manager.matrix.width,
                               self.display_manager.matrix.height),
                       (0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.text((5, 12), "No Stock News", fill=(150, 150, 150))

        self.display_manager.image = img.copy()
        self.display_manager.update_display()

    def _display_error(self, message: str):
        """Display error message."""
        img = Image.new('RGB', (self.display_manager.matrix.width,
                               self.display_manager.matrix.height),
                       (0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.text((5, 12), message, fill=(255, 0, 0))

        self.display_manager.image = img.copy()
        self.display_manager.update_display()

    def get_display_duration(self) -> float:
        """Get display duration from config."""
        return self.display_duration

    def get_info(self) -> Dict[str, Any]:
        """Return plugin info for web UI."""
        info = super().get_info()
        info.update({
            'total_news_items': len(self.all_news_items),
            'stock_symbols': self.feeds_config.get('stock_symbols', []),
            'custom_feeds': list(self.feeds_config.get('custom_feeds', {}).keys()),
            'last_update': self.last_update,
            'display_duration': self.display_duration,
            'scroll_speed': self.scroll_speed,
            'max_headlines_per_symbol': self.max_headlines_per_symbol,
            'headlines_per_rotation': self.headlines_per_rotation,
            'font': self.font_name,
            'news_source': self.news_source,
            'text_color': self.text_color,
            'symbol_color': self.symbol_color,
            'separator_color': self.separator_color
        })
        return info

    def on_config_change(self, new_config: Dict[str, Any]) -> None:
        """Reload instance variables when config is changed via the web UI."""
        super().on_config_change(new_config)

        self.feeds_config = new_config.get('feeds', {})
        self.global_config = new_config.get('global', {})

        # Display settings (type-coerced)
        self.display_duration = self._to_float(self.global_config.get('display_duration', 30), 30)
        self.scroll_speed = self._to_float(self.global_config.get('scroll_speed', 1), 1)
        self.scroll_delay = self._to_float(self.global_config.get('scroll_delay', 0.01), 0.01)
        self.dynamic_duration = bool(self.global_config.get('dynamic_duration', True))
        self.min_duration = self._to_float(self.global_config.get('min_duration', 30), 30)
        self.max_duration = self._to_float(self.global_config.get('max_duration', 300), 300)
        self.max_headlines_per_symbol = self._to_int(
            self.global_config.get('max_headlines_per_symbol', 1), 1)
        self.headlines_per_rotation = self._to_int(
            self.global_config.get('headlines_per_rotation', 2), 2)
        self.font_name = self.global_config.get('font', '10x20')
        self.news_source = self.feeds_config.get('news_source', 'google_news')
        self.market_feeds = self.feeds_config.get('market_feeds', {})

        # Colors (type-coerced)
        self.text_color = self._to_color(
            self.feeds_config.get('text_color', [0, 255, 0]), (0, 255, 0))
        self.symbol_color = self._to_color(
            self.feeds_config.get('symbol_color', [255, 255, 0]), (255, 255, 0))
        self.separator_color = self._to_color(
            self.feeds_config.get('separator_color', [255, 0, 0]), (255, 0, 0))

        # Background service
        self.background_config = self.global_config.get('background_service', {
            'enabled': True,
            'request_timeout': 30,
            'max_retries': 5,
            'priority': 2
        })

        # Recalculate font size and reload fonts
        font_entry = self._BDF_FONT_MAP.get(self.font_name, ('10x20.bdf', 20))
        self.font_size = font_entry[1]
        self._load_fonts()

        # Update scroll speed
        pixels_per_second = (
            self.scroll_speed / self.scroll_delay
            if self.scroll_delay > 0 else self.scroll_speed * 100
        )
        self.scroll_helper.set_scroll_speed(pixels_per_second)
        self.scroll_helper.set_scroll_delay(self.scroll_delay)

        # Re-register fonts and mark scrolling as dirty
        self._register_fonts()
        self._scrolling_dirty = True

        self.logger.info("Stock news ticker config reloaded")

    def cleanup(self) -> None:
        """Cleanup resources."""
        self.all_news_items = []
        self.current_news_items = []
        self.logger.info("Stock news ticker plugin cleaned up")
