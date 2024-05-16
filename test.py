from fastapi import FastAPI, Request
from ib_insync import IB, Stock, Option, MarketOrder
from discord.ext import commands
import discord
from datetime import datetime, timedelta
import asyncio
import logging

app = FastAPI()
bot = commands.Bot(command_prefix="!")

# Initialize IB connection
ib = IB()

# Configuration
config = {
    "sma_entry_condition": True,
    "trade_at_candle_open": True,
    "max_contracts_per_trade": 1,
    "max_stocks_to_trade": 1,
    "account_equity_percentage": 0.5,
    "take_profit_percentage": 0.1,
    "take_profit_amount": 100,
    "max_loss_percentage": 0.1,
    "min_open_interest": 2000,
    "contract_length_days": 5,
    "discord_token": "YOUR_DISCORD_BOT_TOKEN",
    "discord_channel_id": "YOUR_DISCORD_CHANNEL_ID",
}

# Global variables
active_trades = {}
system_active = True

# Alerts module
async def send_alert(message):
    channel = bot.get_channel(config["discord_channel_id"])
    await channel.send(message)

# Options contract execution
async def place_option_order(symbol, direction):
    if not system_active:
        await send_alert("System is not active. Skipping trade.")
        return

    if len(active_trades) >= config["max_stocks_to_trade"]:
        await send_alert(f"Maximum number of stocks ({config['max_stocks_to_trade']}) already being traded. Skipping trade for {symbol}.")
        return

    stock = Stock(symbol, "SMART", "USD")

    # Get current market price
    ib.qualifyContracts(stock)
    current_price = (await ib.reqMktData(stock)).last

    # Calculate strike price based on direction
    if direction == "LONG":
        strike_price = round(current_price)
    else:
        strike_price = round(current_price)

    # Calculate expiration date
    if datetime.today().weekday() <= 2:  # Monday, Tuesday, Wednesday
        expiry = datetime.today() + timedelta(days=config["contract_length_days"])
    else:  # Thursday, Friday
        expiry = datetime.today() + timedelta(days=config["contract_length_days"] + 2)

    # Find the appropriate option contract
    option_contract = Option(symbol, expiry, strike_price, "C" if direction == "LONG" else "P", "SMART")
    ib.qualifyContracts(option_contract)

    # Check open interest
    if (await ib.reqMktData(option_contract)).callOpenInterest < config["min_open_interest"]:
        await send_alert(f"{symbol} - {direction} - Open interest below threshold")
        return

    # Check account equity
    account_summary = ib.accountSummary()
    equity = float(next(summary.value for summary in account_summary if summary.tag == "EquityWithLoanValue"))
    if equity * config["account_equity_percentage"] < option_contract.marketPrice() * config["max_contracts_per_trade"] * 100:
        await send_alert(f"Insufficient account equity to place trade for {symbol}.")
        return

    # Place the order
    order = MarketOrder(action="BUY", totalQuantity=config["max_contracts_per_trade"])
    trade = await ib.placeOrder(option_contract, order)

    # Store the active trade
    active_trades[trade.contract.conId] = trade

    # Send entry alert
    await send_alert(f"{symbol} - {direction} - Entry - {datetime.now()}")

    # Check take profit and stop loss
    while trade.contract.conId in active_trades:
        position = ib.positions(option_contract)
        if not position:
            break

        pnl = position[0].unrealizedPNL
        if pnl >= config["take_profit_amount"] or pnl >= position[0].avgCost * config["take_profit_percentage"]:
            await ib.closePosition(option_contract)
            del active_trades[trade.contract.conId]
            await send_alert(f"{symbol} - {direction} - Take Profit - {datetime.now()}")
            break

        if pnl <= -position[0].avgCost * config["max_loss_percentage"]:
            await ib.closePosition(option_contract)
            del active_trades[trade.contract.conId]
            await send_alert(f"{symbol} - {direction} - Stop Loss - {datetime.now()}")
            break

        await asyncio.sleep(1)

@app.on_event("startup")
async def startup_event():
    await ib.connectAsync("127.0.0.1", 7497, clientId=1)
    await bot.start(config["discord_token"])

@app.on_event("shutdown")
async def shutdown_event():
    ib.disconnect()
    await bot.close()

@app.post("/webhook")
async def handle_webhook(request: Request):
    data = await request.json()
    symbol = data["symbol"]
    direction = data["direction"]

    await place_option_order(symbol, direction)

    return {"status": "success"}

@bot.command(name="open_trades")
async def open_trades(ctx):
    if active_trades:
        message = "Open Trades:\n"
        for trade in active_trades.values():
            message += f"{trade.contract.symbol} - {trade.contract.right} - {trade.contract.lastTradeDateOrContractMonth} - {trade.contract.strike}\n"
        await ctx.send(message)
    else:
        await ctx.send("No open trades.")

@bot.command(name="pnl")
async def pnl(ctx):
    account_summary = ib.accountSummary()
    total_pnl = sum(float(summary.value) for summary in account_summary if summary.tag == "UnrealizedPnL")
    await ctx.send(f"Total Unrealized P/L: {total_pnl}")

@bot.command(name="close_trade")
async def close_trade(ctx, con_id: int):
    if con_id in active_trades:
        trade = active_trades[con_id]
        await ib.closePosition(trade.contract)
        del active_trades[con_id]
        await ctx.send(f"Closed trade for {trade.contract.symbol} - {trade.contract.right} - {trade.contract.lastTradeDateOrContractMonth} - {trade.contract.strike}")
    else:
        await ctx.send(f"No active trade found with contract ID: {con_id}")

@bot.command(name="stop_system")
async def stop_system(ctx):
    global system_active
    system_active = False
    await ctx.send("Trading system stopped.")

@bot.command(name="start_system")
async def start_system(ctx):
    global system_active
    system_active = True
    await ctx.send("Trading system started.")

@bot.command(name="set_config")
async def set_config(ctx, key: str, value: str):
    if key in config:
        original_value = config[key]
        config[key] = type(original_value)(value)
        await ctx.send(f"Configuration updated: {key} = {config[key]}")
    else:
        await ctx.send(f"Invalid configuration key: {key}")

@bot.command(name="get_config")
async def get_config(ctx, key: str):
    if key in config:
        await ctx.send(f"{key} = {config[key]}")
    else:
        await ctx.send(f"Invalid configuration key: {key}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)