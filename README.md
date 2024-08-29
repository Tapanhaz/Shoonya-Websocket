# Shoonya-Websocket
Shoonya websocket based on picows

# Example:
```
import uvloop
import asyncio
import logging
from shoonya_ticker import ShoonyaTicker

logging.basicConfig(level=logging.DEBUG)

def on_tick(msg):
    print(f"tick : {msg}")
def on_order_update(msg):
    print(f"order : {msg}")
def on_error(msg):
    print(f"error : {msg}")
        
username = "Your username here."
token = "Your session token here."
ws_endpoint = "wss://api.shoonya.com/NorenWSTP/"

ticker = ShoonyaTicker(ws_endpoint, username, token)
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
loop = asyncio.get_event_loop()
ticker.start_websocket(
                subscribe_callback= on_tick,
                order_update_callback= on_order_update,
                error_callback= on_error
                )
loop.call_later(5, ticker.subscribe, ["MCX|430106", "MCX|430107", "MCX|430268", "MCX|426265", "MCX|430269", "MCX|435487"])#["NSE|26009", "NSE|26000"])
loop.call_later(10, ticker.close_websocket)
loop.run_forever()```
