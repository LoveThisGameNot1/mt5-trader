"""mt5-trader: a MetaTrader 5 trading skill pack driven over MCP.

The package is split so that every decision the model can influence lives in
a plain function that can be tested without a terminal:

    config       paths, config file, and the hard ceilings nothing can raise
    errors       MetaTrader 5 retcodes turned into text and "may I retry this"
    records      defensive field access for MT5 structseq records
    state        atomic JSON state, intent log, append-only event journal
    bridge       the one thread that is allowed to touch the MetaTrader5 module
    instruments  symbol metadata, and the volume/price/stop normalisation
    sizing       position size from money at risk, R multiples, margin
    risk         the kernel: floors, kill switch, exposure, rate limits
    orders       request building, filling negotiation, retries, reconciliation
    positions    SL/TP, partial close, break-even, trailing, close-all
    history      broker day boundary, realised PnL, statistics
    server       the MCP stdio server
    cli          doctor / status / selftest / serve

Nothing in this package places an order unless the process was started with the
matching environment switch, and nothing in it can raise a hard ceiling.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
