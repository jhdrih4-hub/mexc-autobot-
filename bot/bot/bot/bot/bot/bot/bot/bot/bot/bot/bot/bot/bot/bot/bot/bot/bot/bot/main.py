import sys

from bot.utils import load_env_file


def main():
    load_env_file()
    from bot.config import AppConfig, TradingMode
    from bot.utils import setup_logging, LOG
    cfg = AppConfig.from_env()
    setup_logging(cfg.data_dir)
    LOG.info("autobot starting | mode=%s | exchange=%s | market=%s | top_n=%d",
             cfg.mode.value, cfg.exchange_id, cfg.market_type, cfg.top_n_symbols)
    try:
        if cfg.mode == TradingMode.BACKTEST:
            from bot.backtest import run_backtest
            from bot.exchange import CcxtAdapter
            adapter = CcxtAdapter(cfg.exchange_id, cfg.market_type, cfg.credentials)
            adapter.load_markets()
            run_backtest(cfg, adapter)
            return
        from bot.engine import TradingEngine
        TradingEngine(cfg).run()
    except KeyboardInterrupt:
        LOG.info("interrupted by user")
    except Exception as e:
        LOG.exception("fatal: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
