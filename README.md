# Shoonya-Websocket
Shoonya websocket based on picows : https://github.com/tarasko/picows

# Example:
```

import signal
import asyncio
import logging
import platform
from shoonya_ticker_i import ShoonyaTicker

if platform.system() == "Windows":
    import winloop
    asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
else:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

logging.basicConfig(level=logging.DEBUG)

def on_tick(msg):
    print(f"tick : {msg}")

def on_order_update(msg):
    print(f"order : {msg}")

def on_error(msg):
    print(f"error : {msg}")

if __name__ == "__main__":
    username = "Your username here."
    token = "Your session token here."
    ws_endpoint = "wss://api.shoonya.com/NorenWSTP/"

    ticker = ShoonyaTicker(ws_endpoint, username, token)

    signal.signal(signal.SIGINT, ticker.stop_signal_handler)
    signal.signal(signal.SIGTERM, ticker.stop_signal_handler)

    loop = asyncio.get_event_loop()

    ticker.start_websocket(
                    subscribe_callback= on_tick,
                    order_update_callback= on_order_update,
                    error_callback= on_error
                    )
    
    tokens_list = ["BSE|1", "BSE|12", "NSE|26000", "NSE|26009", "MCX|430106", "MCX|430107"]
    #This will subscribe to tokens after 5 seconds 
    loop.call_later(5, ticker.subscribe, tokens_list)
    # This will close websocket after 20 seconds
    loop.call_later(20, ticker.close_websocket) 
    loop.run_forever()
```
*** Special Thanks to [tarasc](https://github.com/tarasko) for this excellent library and for his valuable guidance *** 
