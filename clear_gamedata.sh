#!/bin/bash

echo "Clearing game data cache files..."

rm -f .pcgw_cache/processed_pageids.json .pcgw_cache/failed_pages.json pcgw_game_data.json

if [ $? -eq 0 ]; then
  echo "Cleanup complete. Files removed successfully."
else
  echo "Warning: Some files might not have been found or removed."
fi
