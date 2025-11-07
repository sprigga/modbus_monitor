#!/usr/bin/env python3
"""
FastAPI Backend for Modbus Monitor
Provides REST API for Modbus operations and Redis data storage
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import asyncio
import redis.asyncio as redis
import json
import logging
from datetime import datetime
import os
from dotenv import load_dotenv

from backend.modbus_service import ModbusService, ModbusConfig

# Load environment variables
load_dotenv()

app = FastAPI(title="Modbus Monitor API", version="1.0.0")

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global variables
modbus_service: Optional[ModbusService] = None
redis_client: Optional[redis.Redis] = None
monitoring_task: Optional[asyncio.Task] = None

# Pydantic models
class ModbusConfigModel(BaseModel):
    host: str
    port: int = 502
    device_id: int = 1
    poll_interval: float = 2.0
    timeout: float = 3.0
    retries: int = 3
    start_address: int = 1
    end_address: int = 26

class RegisterReadRequest(BaseModel):
    address: int
    count: int = 1
    register_type: str = "holding"

class RegisterWriteRequest(BaseModel):
    address: int
    value: int
    
class MultipleRegisterWriteRequest(BaseModel):
    address: int
    values: List[int]

@app.on_event("startup")
async def startup_event():
    """Initialize Redis connection and Modbus service"""
    global redis_client, modbus_service
    
    # Setup logging
    logging.basicConfig(level=logging.INFO)
    
    # Initialize Redis
    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_port = int(os.getenv("REDIS_PORT", 6379))
    redis_client = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
    
    # Initialize Modbus service with config from environment
    config = ModbusConfig(
        host=os.getenv("MODBUS_HOST", "192.168.30.24"),
        port=int(os.getenv("MODBUS_PORT", 502)),
        device_id=int(os.getenv("MODBUS_DEVICE_ID", 1)),
        poll_interval=float(os.getenv("MODBUS_POLL_INTERVAL", 2.0)),
        timeout=float(os.getenv("MODBUS_TIMEOUT", 3.0)),
        retries=int(os.getenv("MODBUS_RETRIES", 3))
    )
    
    modbus_service = ModbusService(config, redis_client)

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup connections"""
    global monitoring_task
    
    if monitoring_task and not monitoring_task.done():
        monitoring_task.cancel()
        try:
            await monitoring_task
        except asyncio.CancelledError:
            pass
    
    if modbus_service:
        await modbus_service.disconnect()
    
    if redis_client:
        await redis_client.close()

@app.get("/api/config")
async def get_config():
    """Get current Modbus configuration"""
    if not modbus_service:
        raise HTTPException(status_code=500, detail="Modbus service not initialized")
    
    return {
        "host": modbus_service.config.host,
        "port": modbus_service.config.port,
        "device_id": modbus_service.config.device_id,
        "poll_interval": modbus_service.config.poll_interval,
        "timeout": modbus_service.config.timeout,
        "retries": modbus_service.config.retries,
        "start_address": int(os.getenv("START_ADDRESS", 1)),
        "end_address": int(os.getenv("END_ADDRESS", 26))
    }

@app.post("/api/config")
async def update_config(config: ModbusConfigModel):
    """Update Modbus configuration"""
    global modbus_service, monitoring_task
    
    # Stop current monitoring if running
    if monitoring_task and not monitoring_task.done():
        monitoring_task.cancel()
        try:
            await monitoring_task
        except asyncio.CancelledError:
            pass
    
    # Disconnect current service
    if modbus_service:
        await modbus_service.disconnect()
    
    # Store start/end address in environment variables (or use a global config store)
    os.environ["START_ADDRESS"] = str(config.start_address)
    os.environ["END_ADDRESS"] = str(config.end_address)
    
    # Create new service with updated config
    new_config = ModbusConfig(
        host=config.host,
        port=config.port,
        device_id=config.device_id,
        poll_interval=config.poll_interval,
        timeout=config.timeout,
        retries=config.retries
    )
    
    modbus_service = ModbusService(new_config, redis_client)
    
    return {"message": "Configuration updated successfully"}

@app.post("/api/connect")
async def connect():
    """Connect to Modbus device"""
    if not modbus_service:
        raise HTTPException(status_code=500, detail="Modbus service not initialized")
    
    success = await modbus_service.connect()
    if success:
        return {"message": "Connected successfully"}
    else:
        raise HTTPException(status_code=400, detail="Failed to connect to Modbus device")

@app.post("/api/disconnect")
async def disconnect():
    """Disconnect from Modbus device"""
    global monitoring_task
    
    if monitoring_task and not monitoring_task.done():
        monitoring_task.cancel()
        try:
            await monitoring_task
        except asyncio.CancelledError:
            pass
    
    if modbus_service:
        await modbus_service.disconnect()
    
    return {"message": "Disconnected successfully"}

@app.get("/api/status")
async def get_status():
    """Get connection status"""
    if not modbus_service:
        return {"connected": False, "monitoring": False}
    
    is_monitoring = monitoring_task and not monitoring_task.done()
    
    return {
        "connected": modbus_service.is_connected(),
        "monitoring": is_monitoring
    }

@app.post("/api/read")
async def read_registers(request: RegisterReadRequest):
    """Read Modbus registers"""
    if not modbus_service:
        raise HTTPException(status_code=500, detail="Modbus service not initialized")
    
    if not modbus_service.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to Modbus device")
    
    result = await modbus_service.read_registers(
        request.address, 
        request.count, 
        request.register_type
    )
    
    if result:
        return result
    else:
        raise HTTPException(status_code=400, detail="Failed to read registers")

@app.post("/api/write")
async def write_register(request: RegisterWriteRequest):
    """Write single holding register"""
    if not modbus_service:
        raise HTTPException(status_code=500, detail="Modbus service not initialized")
    
    if not modbus_service.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to Modbus device")
    
    success = await modbus_service.write_single_register(request.address, request.value)
    
    if success:
        return {"message": f"Successfully wrote value {request.value} to address {request.address}"}
    else:
        raise HTTPException(status_code=400, detail="Failed to write register")

@app.post("/api/write_multiple")
async def write_multiple_registers(request: MultipleRegisterWriteRequest):
    """Write multiple holding registers"""
    if not modbus_service:
        raise HTTPException(status_code=500, detail="Modbus service not initialized")
    
    if not modbus_service.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to Modbus device")
    
    success = await modbus_service.write_multiple_registers(request.address, request.values)
    
    if success:
        return {"message": f"Successfully wrote {len(request.values)} registers starting at address {request.address}"}
    else:
        raise HTTPException(status_code=400, detail="Failed to write registers")

@app.post("/api/start_monitoring")
async def start_monitoring():
    """Start continuous monitoring"""
    global monitoring_task
    
    if not modbus_service:
        raise HTTPException(status_code=500, detail="Modbus service not initialized")
    
    if not modbus_service.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to Modbus device")
    
    if monitoring_task and not monitoring_task.done():
        raise HTTPException(status_code=400, detail="Monitoring already running")
    
    # Setup registers to monitor from environment
    start_addr = int(os.getenv("START_ADDRESS", 1))
    end_addr = int(os.getenv("END_ADDRESS", 26))
    count = end_addr - start_addr + 1
    
    modbus_service.add_register(start_addr, count, "holding", f"Holding_{start_addr}-{end_addr}")
    
    # Start monitoring task
    monitoring_task = asyncio.create_task(modbus_service.start_monitoring())
    
    return {"message": "Monitoring started"}

@app.post("/api/stop_monitoring")
async def stop_monitoring():
    """Stop continuous monitoring"""
    global monitoring_task
    
    if monitoring_task and not monitoring_task.done():
        monitoring_task.cancel()
        try:
            await monitoring_task
        except asyncio.CancelledError:
            pass
    
    if modbus_service:
        modbus_service.stop_monitoring()
    
    return {"message": "Monitoring stopped"}

@app.get("/api/data/latest")
async def get_latest_data():
    """Get latest Modbus data from Redis"""
    if not redis_client:
        raise HTTPException(status_code=500, detail="Redis not available")
    
    try:
        data = await redis_client.get("modbus:latest")
        if data:
            return json.loads(data)
        else:
            return {"message": "No data available"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving data: {str(e)}")

@app.get("/api/data/history")
async def get_historical_data(limit: int = 100):
    """Get historical Modbus data from Redis"""
    if not redis_client:
        raise HTTPException(status_code=500, detail="Redis not available")
    
    try:
        # Get latest entries from sorted set
        data = await redis_client.zrevrange("modbus:history", 0, limit-1, withscores=True)
        
        history = []
        for item, timestamp in data:
            entry = json.loads(item)
            entry['timestamp'] = timestamp
            history.append(entry)
        
        return history
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving history: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)