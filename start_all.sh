#!/bin/bash
python3 hammerdealer_wrapper.py &
python3 ebay_wrapper.py &
python3 kleinanzeigen_wrapper.py &
python3 facebook_wrapper.py &
wait
