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
from typing import Any, Dict, List, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

from src.logging_config import get_logger
from src.plugin_system.base_plugin import BasePlugin


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
            self.global_config.get('max_headlines_per_symbol', 1), 1
        )
        self.headlines_per_rotation = self._to_int(
            self.global_config.get('headlines_per_rotation', 2), 2
        )
        self.font_size = self._to_int(self.global_config.get('font_size', 10), 10)

        # Colors (type-coerced)
        self.text_color = self._to_color(
            self.feeds_config.get('text_color', [0, 255, 0]), (0, 255, 0)
        )
        self.symbol_color = self._to_color(
            self.feeds_config.get('symbol_color', [255, 255, 0]), (255, 255, 0)
        )
        self.separator_color = self._to_color(
            self.feeds_config.get('separator_color', [255, 0, 0]), (255, 0, 0)
        )

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

        # Cached fonts (loaded once, reused in display loop)
        self._headline_font: Optional[ImageFont.ImageFont] = None
        self._info_font: Optional[ImageFont.ImageFont] = None
        self._load_fonts()

        # Register fonts with font manager
        self._register_fonts()

        # Log configuration
        stock_symbols = self.feeds_config.get('stock_symbols', [])
        custom_feeds = list(self.feeds_config.get('custom_feeds', {}).keys())

        self.logger.info("Stock news ticker plugin initialized")
        self.logger.info(f"Tracking symbols: {stock_symbols}")
        self.logger.info(f"Custom feeds: {custom_feeds}")

    def _load_fonts(self) -> None:
        """Load and cache PIL fonts for display rendering.

        Only TTF/OTF fonts can be loaded via ImageFont.truetype().
        BDF bitmap fonts require freetype (handled by the core font
        manager) and are NOT compatible with this fallback chain.
        """
        # Headline / symbol font – try TTF fonts, then PIL default
        for font_path in ('assets/fonts/PressStart2P-Regular.ttf',
                          'assets/fonts/4x6-font.ttf'):
            try:
                self._headline_font = ImageFont.truetype(font_path, self.font_size)
                self.logger.debug("Loaded headline font: %s", font_path)
                break
            except (OSError, IOError):
                continue

        if self._headline_font is None:
            self._headline_font = ImageFont.load_default()
            self.logger.warning("Using PIL default font for headlines")

        # Info font (smaller, for source / timestamp)
        try:
            self._info_font = ImageFont.truetype('assets/fonts/4x6-font.ttf', 8)
        except (OSError, IOError):
            self._info_font = ImageFont.load_default()
            self.logger.warning("Using PIL default font for info text")

    def _register_fonts(self) -> None:
        """Register fonts with the font manager."""
        try:
            if not hasattr(self.plugin_manager, 'font_manager'):
                return

            font_manager = self.plugin_manager.font_manager

            # Headline font
            font_manager.register_manager_font(
                manager_id=self.plugin_id,
                element_key=f"{self.plugin_id}.headline",
                family="press_start",
                size_px=self.font_size,
                color=self.text_color
            )

            # Symbol font
            font_manager.register_manager_font(
                manager_id=self.plugin_id,
                element_key=f"{self.plugin_id}.symbol",
                family="press_start",
                size_px=self.font_size,
                color=self.symbol_color
            )

            # Separator font
            font_manager.register_manager_font(
                manager_id=self.plugin_id,
                element_key=f"{self.plugin_id}.separator",
                family="press_start",
                size_px=self.font_size,
                color=self.separator_color
            )

            # Info font (source, time)
            font_manager.register_manager_font(
                manager_id=self.plugin_id,
                element_key=f"{self.plugin_id}.info",
                family="four_by_six",
                size_px=6,
                color=(150, 150, 150)
            )

            self.logger.info("Stock news ticker fonts registered")
        except (AttributeError, OSError, ValueError) as e:
            self.logger.warning(f"Error registering fonts: {e}")

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

            # Fetch from custom feeds
            custom_feeds = self.feeds_config.get('custom_feeds', {})
            for feed_name, feed_url in custom_feeds.items():
                custom_news = self._fetch_feed_headlines(feed_name, feed_url)
                if custom_news:
                    self.all_news_items.extend(custom_news)

            # Limit total news items
            max_items = len(stock_symbols) * self.max_headlines_per_symbol + len(custom_feeds) * self.headlines_per_rotation
            if len(self.all_news_items) > max_items:
                self.all_news_items = self.all_news_items[:max_items]

            self.last_update = time.time()
            self.logger.debug(f"Updated stock news: {len(self.all_news_items)} total items")

        except (requests.RequestException, KeyError, TypeError, ValueError) as e:
            self.logger.error(f"Error updating stock news: {e}")

    def _fetch_stock_news(self, symbol: str) -> List[Dict]:
        """Fetch news for a specific stock symbol."""
        cache_key = f"stock_news_{symbol}_{datetime.now().strftime('%Y%m%d%H')}"
        update_interval = self.global_config.get('update_interval', 300)

        # Check cache first
        cached_data = self.cache_manager.get(cache_key)
        if cached_data and (time.time() - self.last_update) < update_interval:
            self.logger.debug(f"Using cached news for {symbol}")
            return cached_data

        try:
            # For now, return placeholder data since actual stock news APIs would require API keys
            # In a real implementation, this would call financial news APIs
            placeholder_news = [
                {
                    'symbol': symbol,
                    'title': f"{symbol} Reports Strong Quarterly Earnings",
                    'summary': f"{symbol} announces better than expected results",
                    'source': 'Financial News',
                    'published': datetime.now().isoformat(),
                    'url': f'https://example.com/news/{symbol}'
                }
            ]

            # Cache the results
            self.cache_manager.set(cache_key, placeholder_news, ttl=update_interval * 2)

            return placeholder_news

        except (KeyError, TypeError, ValueError) as e:
            self.logger.error(f"Error fetching news for {symbol}: {e}")
            return []

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
        except (KeyError, TypeError, ValueError, AttributeError) as e:
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
        """
        Display scrolling stock news headlines.

        Args:
            force_clear: If True, clear display before rendering
        """
        if not self.initialized:
            self._display_error("Stock news ticker plugin not initialized")
            return

        if force_clear:
            self.display_manager.clear()

        if not self.all_news_items:
            self._display_no_news()
            return

        # Display scrolling stock news
        self._display_scrolling_stock_news()

    def _display_scrolling_stock_news(self) -> None:
        """Display scrolling stock news headlines."""
        try:
            matrix_width = self.display_manager.matrix.width
            matrix_height = self.display_manager.matrix.height
            font = self._headline_font

            # Create base image
            img = Image.new('RGB', (matrix_width, matrix_height), (0, 0, 0))
            draw = ImageDraw.Draw(img)

            # Display first few news items (placeholder for full scrolling implementation)
            y_offset = 5
            max_items = min(3, len(self.all_news_items))

            for i in range(max_items):
                if i >= len(self.all_news_items):
                    break

                news_item = self.all_news_items[i]

                symbol = news_item.get('symbol', news_item.get('feed_name', 'UNKNOWN'))
                title = news_item.get('title', 'No title')

                # Truncate for display
                if len(title) > 25:
                    title = title[:22] + "..."

                draw.text((5, y_offset), f"{symbol}:", font=font, fill=self.symbol_color)
                draw.text((45, y_offset), title, font=font, fill=self.text_color)

                # Add separator between items
                if i < max_items - 1:
                    separator_y = y_offset + self.font_size + 2
                    draw.text((5, separator_y), "---", font=font, fill=self.separator_color)

                y_offset += self.font_size + 8

            self.display_manager.image = img.copy()
            self.display_manager.update_display()

        except (OSError, TypeError, ValueError, AttributeError) as e:
            self.logger.error(f"Error displaying stock news: {e}")
            self._display_error("Display error")

    def _display_no_news(self) -> None:
        """Display message when no news is available."""
        img = Image.new('RGB', (self.display_manager.matrix.width,
                               self.display_manager.matrix.height),
                       (0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.text((5, 12), "No Stock News", font=self._info_font, fill=(150, 150, 150))

        self.display_manager.image = img.copy()
        self.display_manager.update_display()

    def _display_error(self, message: str) -> None:
        """Display error message."""
        img = Image.new('RGB', (self.display_manager.matrix.width,
                               self.display_manager.matrix.height),
                       (0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.text((5, 12), message, font=self._info_font, fill=(255, 0, 0))

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
            'font_size': self.font_size,
            'text_color': self.text_color,
            'symbol_color': self.symbol_color,
            'separator_color': self.separator_color
        })
        return info

    def cleanup(self) -> None:
        """Cleanup resources."""
        self.all_news_items = []
        self.current_news_items = []
        self._headline_font = None
        self._info_font = None
        self.logger.info("Stock news ticker plugin cleaned up")
