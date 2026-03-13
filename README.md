# 5ymb01 Ticker -- Stock News

> Part of [5ymb01 Ticker](https://github.com/5ymb01/5ymb01-ticker) | Based on [ChuckBuilds/ledmatrix-stock-news](https://github.com/ChuckBuilds/ledmatrix-stock-news)

## Custom Modifications

- **Custom font sources**: Added bitmap font selection (10x20 through 6x10) for headline legibility on LED matrix
- **Type coercion fixes**: Robust handling of config values that arrive as wrong types (string vs int/float)
- **force_clear support**: Proper handling of `force_clear` parameter in `display()` for clean transitions
- **CodeRabbit compliance**: Exception narrowing, input validation, and defensive coding fixes

## Features

- **Stock Symbol Tracking**: Per-symbol news from Yahoo Finance, Google News, or Seeking Alpha RSS
- **General Market Feeds**: Optional CNBC and MarketWatch top stories
- **Scrolling Headlines**: Smooth horizontal scroll with configurable speed and direction
- **Custom RSS Feeds**: Add your own financial feed URLs with custom labels
- **Symbol Highlighting**: Color-coded stock symbols (yellow) distinct from headline text (green)
- **Scroll Modes**: One-shot or continuous scrolling, left or right direction
- **Dynamic Duration**: Auto-adjusts display time based on headline count and content width
- **Background Data Fetching**: Non-blocking RSS parsing with retry and timeout support

## Configuration

Key settings in `config/config.json` under `ledmatrix-stock-news`:

| Setting | Default | Description |
|---------|---------|-------------|
| `feeds.stock_symbols` | `["AAPL","GOOGL","MSFT"]` | Symbols to fetch news for |
| `feeds.news_source` | `"google_news"` | RSS provider: yahoo/google_news/seeking_alpha |
| `feeds.market_feeds.cnbc` | `false` | Include CNBC top news |
| `feeds.market_feeds.marketwatch` | `false` | Include MarketWatch stories |
| `feeds.custom_feeds` | `{}` | Custom RSS URLs (`{"Label": "https://..."}`) |
| `feeds.text_color` | `[0,255,0]` | Headline text RGB color |
| `feeds.symbol_color` | `[255,255,0]` | Stock symbol RGB color |
| `global.display_duration` | `30` | Display time (seconds) |
| `global.scroll_speed` | `1` | Scroll speed multiplier |
| `global.font` | `"10x20"` | Bitmap font (WxH format) |
| `global.max_headlines_per_symbol` | `1` | Headlines per symbol (1-5) |
| `global.scroll_mode` | `"one_shot"` | one_shot or continuous |

Works alongside the stocks plugin for a combined financial display -- tickers cycle while news scrolls.

---
*Credits: [ChuckBuilds](https://github.com/ChuckBuilds) (original), [Claude Code](https://claude.ai/claude-code) (AI-assisted development)*
