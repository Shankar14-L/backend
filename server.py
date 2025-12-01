# server.py
# (This is your original server.py with small targeted patches described below)
from fastapi import FastAPI, APIRouter, HTTPException, Depends, status, Body, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from collections import defaultdict
import time
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional, Any, Dict
import uuid
from datetime import datetime, timedelta, timezone
import jwt
import hashlib
import json
import qrcode
import io
import base64
from passlib.context import CryptContext
import secrets
import time
import asyncio
import httpx 
import subprocess
import json as _json
import time
import logging
from fastapi import UploadFile, File, Body
from starlette.responses import JSONResponse, Response

from fastapi.responses import RedirectResponse


# Load environment variables
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

# ==================== MONGODB SERIALIZATION FIX ====================
def serialize_doc(doc):
    """Convert MongoDB document to JSON-serializable dict"""
    if doc is None:
        return None
    if isinstance(doc, list):
        return [serialize_doc(d) for d in doc]
    if isinstance(doc, dict):
        result = {}
        for k, v in doc.items():
            if k == "_id":
                continue  # Skip MongoDB _id
            elif isinstance(v, ObjectId):
                result[k] = str(v)
            elif isinstance(v, datetime):
                result[k] = v.isoformat() if v.tzinfo else v.replace(tzinfo=timezone.utc).isoformat()
            elif isinstance(v, dict):
                result[k] = serialize_doc(v)
            elif isinstance(v, list):
                result[k] = [serialize_doc(i) if isinstance(i, (dict, ObjectId)) else i for i in v]
            else:
                result[k] = v
        return result
    if isinstance(doc, ObjectId):
        return str(doc)
    return doc


logger = logging.getLogger(__name__)

# ==================== IPFS INTEGRATION ====================

logger = logging.getLogger(__name__)

IPFS_API_URL = os.environ.get("IPFS_API_URL", "http://127.0.0.1:5001")
IPFS_GATEWAY_URL = os.environ.get("IPFS_GATEWAY_URL", "http://127.0.0.1:8080")
USE_IPFS = os.environ.get("USE_IPFS", "true").lower() == "true"


async def upload_to_ipfs(data: dict) -> Optional[str]:
    """
    Upload JSON data to IPFS and return CID (tries Pinata then local node).
    Returns CID string on success or None on failure.
    """
    if not USE_IPFS:
        logger.debug("USE_IPFS is false; skipping upload_to_ipfs")
        return None

    try:
        # canonical JSON bytes (sorted keys)
        json_bytes = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")

        async with httpx.AsyncClient(timeout=30.0) as client:
            # 1) Try Pinata if configured
            pinata_jwt = os.environ.get("PINATA_JWT")
            if pinata_jwt:
                try:
                    resp = await client.post(
                        "https://api.pinata.cloud/pinning/pinJSONToIPFS",
                        headers={
                            "Authorization": f"Bearer {pinata_jwt}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "pinataContent": data,
                            "pinataMetadata": {"name": f"attendance_{int(time.time())}"},
                        },
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    # Pinata uses "IpfsHash"
                    ipfs_hash = body.get("IpfsHash") or body.get("IpfsHash")
                    if ipfs_hash:
                        logger.info("Uploaded to Pinata -> %s", ipfs_hash)
                        return ipfs_hash
                except Exception as e:
                    logger.info("Pinata upload failed (falling back): %s", e)

            # 2) Fallback to local IPFS node via /api/v0/add (multipart/form-data)
            file_obj = io.BytesIO(json_bytes)
            file_obj.seek(0)
            files = {"file": ("data.json", file_obj, "application/json")}

            resp = await client.post(f"{IPFS_API_URL}/api/v0/add?pin=true", files=files)
            resp.raise_for_status()

            text = resp.text or ""
            text = text.strip()
            if not text:
                logger.warning("Empty response from ipfs add")
                return None

            # ipfs add returns NDJSON-style lines; parse each line and prefer last Hash/Cid
            ipfs_hash = None
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    # skip non-json lines
                    continue

                # older IPFS responses: {"Name":"...","Hash":"Qm...","Size":"..."}
                if "Hash" in obj and obj["Hash"]:
                    ipfs_hash = obj["Hash"]

                # newer responses: {"Name":"...","Cid":{"/":"bafy..."},"Size":"..."} or "Cid":"bafy..."
                elif "Cid" in obj:
                    cid_val = obj["Cid"]
                    if isinstance(cid_val, dict):
                        ipfs_hash = cid_val.get("/")
                    elif isinstance(cid_val, str):
                        ipfs_hash = cid_val

            if ipfs_hash:
                logger.info("Uploaded to local IPFS -> %s", ipfs_hash)
            else:
                logger.error("Could not parse CID from ipfs add response: %s", text)

            return ipfs_hash

    except httpx.RequestError as re:
        logger.warning("HTTP error during IPFS upload: %s", re)
    except Exception as e:
        logger.exception("Unexpected error uploading to IPFS: %s", e)

    return None


async def get_from_ipfs(cid: str) -> Optional[dict]:
    """
    Retrieve JSON data from IPFS by CID. Tries a public gateway then the configured local gateway.
    Returns parsed JSON (dict) on success or None on failure.
    """
    if not cid:
        return None

    gateways = [
        f"https://gateway.pinata.cloud/ipfs/{cid}",
        f"https://ipfs.io/ipfs/{cid}",
        f"{IPFS_GATEWAY_URL}/ipfs/{cid}",
    ]

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for url in gateways:
                try:
                    resp = await client.get(url)
                except httpx.RequestError as re:
                    logger.debug("Gateway %s request failed: %s", url, re)
                    continue

                if resp.status_code != 200:
                    logger.debug("Gateway %s returned status %s", url, resp.status_code)
                    continue

                text = resp.text or ""
                text = text.strip()
                if not text:
                    logger.debug("Empty body from gateway %s", url)
                    continue

                # Try JSON first, then try to parse text
                try:
                    return resp.json()
                except Exception:
                    try:
                        return json.loads(text)
                    except Exception:
                        # not JSON — return raw text wrapped in a dict maybe, or continue
                        logger.debug("Non-JSON body from %s; returning raw text", url)
                        return {"raw": text, "gateway": url}

    except Exception as e:
        logger.warning("IPFS retrieval error: %s", e)

    return None


# ==================== ETHEREUM INTEGRATION ====================
async def call_node_eth(action, payload):
    """Call Ethereum runner script (cross-platform, avoids asyncio.create_subprocess_exec on Windows)."""

    possible_paths = [
        os.path.join(ROOT_DIR, "server", "eth_runner.js"),
        os.path.join(ROOT_DIR.parent, "server", "eth_runner.js"),
        os.path.join(str(Path.cwd()), "server", "eth_runner.js"),
        os.path.join(str(Path.cwd()), "eth_runner.js"),
    ]

    runner_path = None
    for p in possible_paths:
        if os.path.exists(p):
            runner_path = p
            break

    if not runner_path:
        logger.warning("eth_runner.js not found (looked in %s)", possible_paths)
        return None

    # Build the command
    cmd = ["node", runner_path, action, json.dumps(payload)]

    try:
        # Run subprocess.run in a thread to avoid platform-specific asyncio subprocess issues
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False  # we"ll inspect returncode manually
        )

        out_s = (result.stdout or "").strip()
        err_s = (result.stderr or "").strip()

        if result.returncode == 0 and out_s:
            try:
                return json.loads(out_s)
            except json.JSONDecodeError:
                logger.exception("Ethereum runner returned non-JSON stdout: %s", out_s)
                return None

        if result.returncode != 0:
            logger.error("Ethereum runner exited with code %s. stderr: %s stdout: %s", result.returncode, err_s, out_s)
        elif err_s:
            logger.warning("Ethereum runner stderr (exit 0): %s", err_s)

        return None

    except Exception:
        logger.exception("Ethereum call error while invoking eth_runner.js")
        return None

# ==================== DATABASE SETUP ====================
mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ.get("DB_NAME", "blockchain_attendance")]

# ==================== SECURITY SETUP ====================
security = HTTPBearer()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET_KEY = os.environ.get("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

# ==================== FASTAPI APP ====================
app = FastAPI(title="Blockchain QR Attendance System", version="1.0.0")
api_router = APIRouter(prefix="/api")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ==================== UTILITY FUNCTIONS ====================
def get_utc_now():
    return datetime.now(timezone.utc)

def ensure_tz(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except:
            return None
    if hasattr(dt, "tzinfo") and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def hash_password(password: str) -> str:
    return pwd_context.hash(password[:72])

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain[:72], hashed)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = get_utc_now() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def generate_course_code():
    return "CS" + "".join(secrets.choice("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4))

def calculate_hash(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()

def generate_qr_code(data: str) -> str:
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode()

# ==================== MODELS ====================
class User(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    email: EmailStr
    password: str
    role: str = Field(default="student", pattern="^(student|teacher|admin)$")
    walletAddress: Optional[str] = None
    created_at: datetime = Field(default_factory=get_utc_now)
    # New Student Fields
    dob: Optional[str] = None
    rollNo: Optional[str] = None
    batchYear: Optional[str] = None
    program: Optional[str] = None
    academicYear: Optional[str] = None

class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    role: str = "student"

class RegisterRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    # New Student Fields
    dob: Optional[str] = None
    rollNo: Optional[str] = None
    batchYear: Optional[str] = None
    program: Optional[str] = None
    academicYear: Optional[str] = None

class Class(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    code: str
    teacher_id: str
    students_enrolled: List[str] = []
    created_at: datetime = Field(default_factory=get_utc_now)

class AttendanceRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    student_id: str
    class_id: str
    timestamp: datetime = Field(default_factory=get_utc_now)
    qr_code_id: str
    verified: bool = False
    blockchain_hash: Optional[str] = None
    blockchain_tx: Optional[str] = None
    ipfs_cid: Optional[str] = None

class AttendanceMark(BaseModel):
    qr_content: str

# ==================== AUTHENTICATION ====================
async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await db.users.find_one({"email": email})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return serialize_doc(user)

@api_router.put("/user/profile")
async def update_user_profile(
    profile_data: dict = Body(...),
    current_user: dict = Depends(get_current_user)
):
    """Update user profile information."""
    user_id = current_user["id"]
    
    # Filter out sensitive or immutable fields
    update_fields = {
        k: v for k, v in profile_data.items() 
        if k not in ["id", "email", "password", "role", "walletAddress"]
    }
    
    if not update_fields:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    # Update the user document
    result = await db.users.update_one(
        {"id": user_id},
        {"$set": update_fields}
    )

    if result.modified_count == 0:
        # Check if user exists but no change was made
        if await db.users.find_one({"id": user_id}):
            return serialize_doc(current_user) # Return current data if no change
        raise HTTPException(status_code=404, detail="User not found")

    # Fetch the updated user document
    updated_user = await db.users.find_one({"id": user_id}, {"password": 0})
    return serialize_doc(updated_user)

@api_router.post("/user/update-wallet")
async def update_user_wallet(
    wallet_data: dict = Body(...),
    current_user: dict = Depends(get_current_user)
):
    """Update user"s wallet address."""
    wallet_address = wallet_data.get("walletAddress")
    if not wallet_address:
        raise HTTPException(status_code=400, detail="walletAddress is required")

    # Basic validation for Ethereum address format
    if not wallet_address.startswith("0x") or len(wallet_address) != 42:
        raise HTTPException(status_code=400, detail="Invalid wallet address format")

    result = await db.users.update_one(
        {"id": current_user["id"]},
        {"$set": {"walletAddress": wallet_address}}
    )

    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="User not found or wallet address already set")

    # Fetch the updated user document
    updated_user = await db.users.find_one({"id": current_user["id"]}, {"password": 0})
    return serialize_doc(updated_user)

@api_router.get("/dashboard/student-stats")
async def get_student_stats(current_user: dict = Depends(get_current_user)):
    """Get statistics for the student dashboard."""
    if current_user["role"] != "student":
        raise HTTPException(status_code=403, detail="Access denied")

    student_id = current_user["id"]
    
    # 1. Total Attendance
    total_attendance = await db.attendance.count_documents({"student_id": student_id})

    # 2. Enrolled Classes
    enrolled_classes_cursor = db.classes.find({"students_enrolled": student_id})
    enrolled_classes = await enrolled_classes_cursor.to_list(None)
    classes_enrolled_count = len(enrolled_classes)

    # 3. Attendance Percentage (Placeholder logic)
    unique_classes_attended = await db.attendance.distinct("class_id", {"student_id": student_id})
    attendance_percentage = (len(unique_classes_attended) / classes_enrolled_count) * 100 if classes_enrolled_count > 0 else 0
    attendance_percentage = round(attendance_percentage, 2)

    # 4. Recent Activity (last 5 attendance records)
    recent_attendance = await db.attendance.find({"student_id": student_id}).sort("timestamp", -1).limit(5).to_list(5)
    
    return {
        "total_attendance": total_attendance,
        "enrolled_classes": classes_enrolled_count,
        "attendance_percentage": attendance_percentage,
        "recent_attendance": serialize_doc(recent_attendance)
    }

@api_router.get("/dashboard/teacher-stats")
async def get_teacher_stats(current_user: dict = Depends(get_current_user)):
    """Get statistics for the teacher dashboard."""
    if current_user["role"] != "teacher":
        raise HTTPException(status_code=403, detail="Access denied")

    teacher_id = current_user["id"]
    
    # 1. Total Classes
    teacher_classes = await db.classes.find({"teacher_id": teacher_id}).to_list(None)
    total_classes = len(teacher_classes)
    class_ids = [cls["id"] for cls in teacher_classes]

    # 2. Total Students (unique students across all classes)
    all_students_ids = set()
    for cls in teacher_classes:
        all_students_ids.update(cls.get("students_enrolled", []))
    total_students = len(all_students_ids)

    # 3. Recent Attendance (last 5 attendance records across all their classes)
    recent_attendance = await db.attendance.find({"class_id": {"$in": class_ids}}).sort("timestamp", -1).limit(5).to_list(5)

    # 4. Active QR Codes (QR codes generated by this teacher that are still active)
    active_qrcodes = await db.qr_codes.count_documents({
        "class_id": {"$in": class_ids},
        "is_active": True,
        "expires_at": {"$gt": get_utc_now()}
    })

    return {
        "total_classes": total_classes,
        "total_students": total_students,
        "recent_attendance": serialize_doc(recent_attendance),
        "active_qrcodes": active_qrcodes
    }

# server.py - FIXED REGISTRATION ENDPOINT
# Replace the @api_router.post("/auth/register") section with this:

@api_router.post("/auth/register")
async def register_user(user_data: RegisterRequest):
    """Register a new student and automatically log them in."""
    existing_user = await db.users.find_one({"email": user_data.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")

    hashed_password = hash_password(user_data.password)
    user = User(
        name=user_data.name,
        email=user_data.email,
        password=hashed_password,
        role="student",  # Enforce student role for this endpoint
        dob=user_data.dob,
        rollNo=user_data.rollNo,
        batchYear=user_data.batchYear,
        program=user_data.program,
        academicYear=user_data.academicYear
    )

    # Insert user document
    await db.users.insert_one(user.dict())
    
    # Generate access token for auto-login
    access_token = create_access_token(
        data={"sub": user.email},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    
    # Prepare user data (remove password)
    user_data_dict = user.dict()
    user_data_dict.pop('password', None)
    user_serialized = serialize_doc(user_data_dict)
    
    # Return success with token and user data (same format as login)
    return {
        "success": True,
        "message": "Registration successful! Logging you in...",
        "access_token": access_token,
        "token_type": "bearer",
        "user": user_serialized
    }


@api_router.post("/auth/login")
async def login_for_access_token(form_data: LoginRequest):
    user = await db.users.find_one({"email": form_data.email, "role": form_data.role})
    if not user or not verify_password(form_data.password, user["password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token = create_access_token(
        data={"sub": user["email"]},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    
    return {"access_token": access_token, "token_type": "bearer", "user": serialize_doc(user)}

# ==================== CLASS ROUTES ====================
@api_router.post("/classes/create")
async def create_class(class_data: dict = Body(...), current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can create classes")
    
    new_class = Class(
        name=class_data["name"],
        code=class_data["code"],
        teacher_id=current_user["id"]
    )
    
    await db.classes.insert_one(new_class.dict())
    return serialize_doc(new_class.dict())

@api_router.get("/classes")
async def get_classes(current_user: dict = Depends(get_current_user)):
    if current_user["role"] == "teacher":
        classes = await db.classes.find({"teacher_id": current_user["id"]}).to_list(100)
    else:
        classes = await db.classes.find({"students_enrolled": current_user["id"]}).to_list(100)
    return serialize_doc(classes)

@api_router.post("/attendance/generate-qr")
async def generate_attendance_qr(class_data: dict = Body(...), current_user: dict = Depends(get_current_user)):
    class_id = class_data.get("class_id")
    if not class_id:
        raise HTTPException(status_code=400, detail="class_id is required")

    cls = await db.classes.find_one({"id": class_id, "teacher_id": current_user["id"]})
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found or you are not the teacher")

    # create QR metadata
    qr_id = str(uuid.uuid4())
    expires_at = get_utc_now() + timedelta(minutes=5)
    qr_content = f"{class_id}|{qr_id}|{int(expires_at.timestamp())}"
    qr_image = generate_qr_code(qr_content)

    await db.qr_codes.insert_one({
        "id": qr_id,
        "class_id": class_id,
        "teacher_id": current_user["id"],
        "content": qr_content,
        "created_at": get_utc_now(),
        "expires_at": expires_at,
        "is_active": True
    })

    # --- create on-chain session ---
    try:
        # Build payload that eth_runner expects (camelCase)
        eth_payload = {
            "sessionCode": qr_id,         # use qr_id as the on-chain session code
            "classId": class_id,
            "durationMinutes": 5          # match the QR expiry (minutes)
        }

        logger.info("Creating blockchain session with payload: %s", eth_payload)

        # call_node_eth is async; use await (we are inside async fn)
        eth_create = await call_node_eth("createSession", eth_payload)

        if not eth_create or not eth_create.get("success"):
            logger.error("Failed to create on-chain session for QR %s: %s", qr_id, eth_create)
            # mark qr as inactive since blockchain session not created
            await db.qr_codes.update_one({"id": qr_id}, {"$set": {"is_active": False}})
            raise HTTPException(status_code=500, detail="Failed to create blockchain session for QR")
    except Exception as e:
        logger.exception("Error creating on-chain session: %s", e)
        await db.qr_codes.update_one({"id": qr_id}, {"$set": {"is_active": False}})
        raise HTTPException(status_code=500, detail="Failed to create blockchain session for QR")
    # --- end create session ---
    
    # Return response matching your frontend expectations
    return {
        "qr_code": qr_image,           # Base64 PNG image
        "qr_base64": qr_image,         # Alternative key
        "qr_id": qr_id,                # QR code ID
        "session_id": qr_id,           # Session ID (same as qr_id)
        "sessionId": qr_id,            # Alternative key
        "expires_at": expires_at.isoformat(),  # ISO format string
        "class_name": cls["name"],     # Class name for display
        "class_id": class_id           # Class ID
    }

@api_router.get("/classes/{class_id}/students")
async def get_class_students(class_id: str, current_user: dict = Depends(get_current_user)):
    cls = await db.classes.find_one({"id": class_id})
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")
    
    student_ids = cls.get("students_enrolled", [])
    if not student_ids:
        return []
    
    students = await db.users.find({"id": {"$in": student_ids}}, {"password": 0}).to_list(1000)
    return [serialize_doc(s) for s in students]

@api_router.post("/classes/{class_id}/enroll")
async def enroll_student(class_id: str, student_email: str = Body(..., embed=True), current_user: dict = Depends(get_current_user)):
    student = await db.users.find_one({"email": student_email})
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    await db.classes.update_one({"id": class_id}, {"$addToSet": {"students_enrolled": student["id"]}})
    return {"message": "Student enrolled"}

# ==================== ATTENDANCE ROUTES ====================
@api_router.get("/attendance")
async def query_attendance(
    class_id: Optional[str] = Query(None),
    student_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user)
):
    query = {}
    if class_id:
        query["class_id"] = class_id
    if student_id:
        query["student_id"] = student_id
    
    if not query:
        # Check if teacher
        teacher_classes = await db.classes.find({"teacher_id": current_user["id"]}).to_list(100)
        if teacher_classes:
            class_ids = [c["id"] for c in teacher_classes]
            query["class_id"] = {"$in": class_ids}
        else:
            query["student_id"] = current_user["id"]
    
    records = await db.attendance.find(query).sort("timestamp", -1).to_list(1000)
    
    result = []
    for r in records:
        student = await db.users.find_one({"id": r.get("student_id")})
        serialized = serialize_doc(r)
        serialized["student_wallet"] = student.get("walletAddress", "") if student else ""
        serialized["metamask_address"] = serialized["student_wallet"]
        result.append(serialized)
    
    return result

@api_router.get("/attendance/my")
async def get_my_attendance(current_user: dict = Depends(get_current_user)):
    """Get attendance for current student - FOR GRAPHS"""
    records = await db.attendance.find({"student_id": current_user["id"]}).sort("timestamp", -1).to_list(1000)
    
    result = []
    for r in records:
        ts = ensure_tz(r.get("timestamp"))
        result.append({
            "id": r.get("id"),
            "student_id": r.get("student_id"),
            "student_name": r.get("student_name"),
            "class_id": r.get("class_id"),
            "class_name": r.get("class_name"),
            "timestamp": ts.isoformat() if ts else None,
            "blockchain_hash": r.get("blockchain_hash"),
            "blockchain_tx": r.get("blockchain_tx"),
            "ipfs_cid": r.get("ipfs_cid"),
            "qr_code_id": r.get("qr_code_id"),
            "verified": r.get("verified", True)
        })
    
    return result

@api_router.post("/attendance/mark")
async def mark_attendance(attendance_data: AttendanceMark, current_user: dict = Depends(get_current_user)):
    """Mark attendance using QR code"""
    qr_content = attendance_data.qr_content
    
    # Parse QR content
    try:
        parts = qr_content.split("|")
        if len(parts) != 3:
            raise ValueError()
        class_id, qr_id, _ = parts
    except:
        raise HTTPException(status_code=400, detail="Invalid QR code format")
    
    # Verify QR code
    qr_record = await db.qr_codes.find_one({"id": qr_id, "is_active": True})
    if not qr_record:
        raise HTTPException(status_code=400, detail="Invalid or expired QR code")
     
    expires_at = ensure_tz(qr_record["expires_at"])
    if get_utc_now() > expires_at:
        await db.qr_codes.update_one({"id": qr_id}, {"$set": {"is_active": False}})
        raise HTTPException(status_code=400, detail="QR code expired")
    
    # Check duplicate
    existing = await db.attendance.find_one({
         "student_id": current_user["id"],
         "class_id": class_id,
         "qr_code_id": qr_id
     })
    if existing:
        # Return a specific success status for duplicate attendance
        return JSONResponse(
            status_code=200,
            content={"message": "Attendance already marked for this session", "status": "duplicate"}
        )
    
    # Get class
    cls = await db.classes.find_one({"id": class_id})
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Auto-enroll
    if current_user["id"] not in cls.get("students_enrolled", []):
        await db.classes.update_one(
            {"id": class_id},
            {"$addToSet": {"students_enrolled": current_user["id"]}}
        )
    
    # Create attendance data
    att_id = str(uuid.uuid4())
    timestamp = get_utc_now()
    
    attendance_doc = {
        "id": att_id,
        "student_id": current_user["id"],
        "student_name": current_user["name"],
        "student_wallet": current_user.get("walletAddress", ""),
        "class_id": class_id,
        "class_name": cls["name"],
        "qr_code_id": qr_id,
        "timestamp": timestamp,
        "verified": True
    }
    
    # Create blockchain record
    block_data = {
        "action": "attendance_marked",
        "attendance_id": att_id,
        "student_id": current_user["id"],
        "student_wallet": current_user.get("walletAddress", ""),
        "class_id": class_id,
        "timestamp": timestamp.isoformat()
    }
    
    last_block = await db.blockchain.find().sort("block_number", -1).limit(1).to_list(1)
    prev_hash = last_block[0]["hash"] if last_block else "0"
    block_num = (last_block[0]["block_number"] + 1) if last_block else 1
    
    block_hash = calculate_hash(json.dumps(block_data, sort_keys=True))
    attendance_doc["blockchain_hash"] = block_hash
    
    # Upload to IPFS
    ipfs_data = {
        "type": "attendance_record",
        "attendance_id": att_id,
        "student_id": current_user["id"],
        "student_name": current_user["name"],
        "student_wallet": current_user.get("walletAddress", ""),
        "class_id": class_id,
        "class_name": cls["name"],
        "timestamp": timestamp.isoformat(),
        "blockchain_hash": block_hash
    }
    
    ipfs_cid = await upload_to_ipfs(ipfs_data)
    logger.info("IPFS upload result for attendance %s -> %s", att_id, ipfs_cid)

    if ipfs_cid:
        attendance_doc["ipfs_cid"] = ipfs_cid
        block_data["ipfs_cid"] = ipfs_cid
    
    # Call Ethereum contract
    # Use qr_id as the canonical on-chain sessionCode created when QR was generated
    session_code = qr_id

    # Pre-check session validity on-chain
    try:
        is_valid_res = await call_node_eth("isSessionValid", {"sessionCode": session_code})
        if not is_valid_res or not is_valid_res.get("success") or not is_valid_res.get("isValid"):
            raise HTTPException(status_code=400, detail="Session is invalid or expired")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error checking session validity: %s", e)
        raise HTTPException(status_code=500, detail="Error validating session")

    eth_result = await call_node_eth("markAttendance", {
        "sessionCode": session_code,
        "studentId": current_user["id"],
        "classId": class_id
    })
    
    if eth_result and eth_result.get("txHash"):
        attendance_doc["blockchain_tx"] = eth_result["txHash"]
    
    # Save blockchain block
    block_doc = {
        "id": str(uuid.uuid4()),
        "block_number": block_num,
        "hash": block_hash,
        "previous_hash": prev_hash,
        "data": block_data,
        "timestamp": timestamp,
        "nonce": secrets.randbelow(1000000)
    }
    await db.blockchain.insert_one(block_doc)
    
    # Save attendance
    await db.attendance.insert_one(attendance_doc)
    
    return {
        "status": "success",
        "message": "Attendance marked successfully",
        "blockchain_hash": block_hash,
        "ipfs_cid": ipfs_cid,
        "blockchain_tx": eth_result.get("txHash") if eth_result else None
    }

@api_router.get("/attendance/history")
async def get_attendance_history(current_user: dict = Depends(get_current_user)):
    """Get detailed attendance history for the current user."""
    records = await db.attendance.find({"student_id": current_user["id"]}).sort("timestamp", -1).to_list(1000)
    return serialize_doc(records)

@api_router.get("/attendance/class/{class_id}")
async def get_class_attendance(class_id: str, current_user: dict = Depends(get_current_user)):
    """Get all attendance records for a specific class."""
    if current_user["role"] != "teacher":
        raise HTTPException(status_code=403, detail="Access denied")

    cls = await db.classes.find_one({"id": class_id, "teacher_id": current_user["id"]})
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found or you are not the teacher")

    records = await db.attendance.find({"class_id": class_id}).sort("timestamp", -1).to_list(None)
    return serialize_doc(records)

@api_router.get("/attendance/class/{class_id}/export-csv")
async def export_class_attendance_csv(class_id: str, current_user: dict = Depends(get_current_user)):
    """Export all attendance records for a specific class as CSV."""
    if current_user["role"] != "teacher":
        raise HTTPException(status_code=403, detail="Access denied")

    cls = await db.classes.find_one({"id": class_id, "teacher_id": current_user["id"]})
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found or you are not the teacher")

    # 1. Fetch all attendance records for the class
    attendance_records = await db.attendance.find({"class_id": class_id}).sort("timestamp", -1).to_list(None)
    
    if not attendance_records:
        return Response(content="No attendance records found for this class.", media_type="text/plain")

    # 2. Get unique student IDs
    student_ids = list(set(r["student_id"] for r in attendance_records))
    
    # 3. Fetch student profiles in one go
    students = await db.users.find({"id": {"$in": student_ids}}).to_list(None)
    student_map = {s["id"]: s for s in students}

    # 4. Prepare CSV content
    csv_data = []
    # Header row
    csv_data.append("Name,Roll Number,Email,Timestamp,Blockchain Hash,QR Code ID,Class Name")

    for record in attendance_records:
        student = student_map.get(record["student_id"], {})
        
        name = student.get("name", "N/A")
        # Assuming 'rollNumber' is stored in the user profile, falling back to student_id
        roll_number = student.get("rollNumber") or student.get("student_id", "N/A")
        email = student.get("email", "N/A")
        timestamp = record.get("timestamp").isoformat() if record.get("timestamp") else "N/A"
        blockchain_hash = record.get("blockchain_hash", "N/A")
        qr_code_id = record.get("qr_code_id", "N/A")
        class_name = record.get("class_name", "N/A")

        # Escape commas in fields like name or class name if necessary, though unlikely here
        row = f'"{name}","{roll_number}","{email}","{timestamp}","{blockchain_hash}","{qr_code_id}","{class_name}"'
        csv_data.append(row)

    csv_content = "\n".join(csv_data)

    # 5. Return CSV response
    return Response(
        content=csv_content, 
        media_type="text/csv", 
        headers={"Content-Disposition": f"attachment; filename=attendance_export_{class_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"}
    )
    
# ==================== APP SETUP ====================

# ==================== APP SETUP ====================
app.include_router(api_router)

# CORS Middleware
# ==================== SECURITY MIDDLEWARE ====================

# Simple in-memory rate limiter
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 10  # max requests per window
rate_limit_store = defaultdict(lambda: {"count": 0, "reset_time": 0})

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Apply rate limiting only to auth endpoints
        if request.url.path in ["/api/auth/register", "/api/auth/login"]:
            # Use X-Forwarded-For if available, otherwise client.host
            client_ip = request.headers.get("X-Forwarded-For") or request.client.host
            current_time = time.time()
            
            # Check if the window has reset
            if current_time > rate_limit_store[client_ip]["reset_time"]:
                rate_limit_store[client_ip]["count"] = 0
                rate_limit_store[client_ip]["reset_time"] = current_time + RATE_LIMIT_WINDOW
            
            # Check limit
            if rate_limit_store[client_ip]["count"] >= RATE_LIMIT_MAX_REQUESTS:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Try again in a minute."},
                    headers={"Retry-After": str(int(rate_limit_store[client_ip]["reset_time"] - current_time))}
                )
            
            # Increment count
            rate_limit_store[client_ip]["count"] += 1

        response = await call_next(request)
        return response

app.add_middleware(RateLimitMiddleware)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
from pymongo.errors import OperationFailure
import logging

logger = logging.getLogger(__name__)

async def ensure_unique_index(db, collection_name: str, field: str):
    """
    Ensure a unique index exists on `collection_name`.`field`.
    - If index exists and is unique, do nothing.
    - If index exists and not unique, check for duplicates; raise if duplicates exist.
      If no duplicates, drop existing index and create unique index.
    - If no index exists, create unique index.
    """
    coll = getattr(db, collection_name)
    indexes = await coll.index_information()
    existing_name = None
    existing_spec = None

    # Find any index whose key is field:1
    for name, spec in indexes.items():
        key = spec.get("key")
        # Motor typically returns list of tuples: [('id', 1)]
        if key == [(field, 1)] or key == {field: 1}:
            existing_name = name
            existing_spec = spec
            break

    if existing_name:
        if existing_spec.get("unique", False):
            logger.info("%s.%s index already exists and is unique (name=%s).",
                        collection_name, field, existing_name)
            return
        # non-unique index exists — check for duplicates
        dup = await coll.aggregate([
            {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 1}}},
            {"$limit": 1}
        ]).to_list(length=1)

        if dup:
            # Explicit failure so you can resolve duplicates before proceeding.
            raise RuntimeError(
                f"Cannot convert {collection_name}.{field} index to unique: duplicate values exist. "
                f"Example duplicate group: {dup[0]}"
            )

        # safe to drop and recreate as unique
        logger.info("Dropping existing index %s on %s.%s and creating unique index.",
                    existing_name, collection_name, field)
        await coll.drop_index(existing_name)
        await coll.create_index(field, unique=True)
        logger.info("Unique index created on %s.%s", collection_name, field)
    else:
        logger.info("Creating unique index on %s.%s", collection_name, field)
        await coll.create_index(field, unique=True)
        logger.info("Unique index created on %s.%s", collection_name, field)


@app.on_event("startup")
async def startup_event():
    # Create indexes for faster queries
    await db.users.create_index("email", unique=True)
    await db.users.create_index("id", unique=True)
    await db.classes.create_index("id", unique=True)
    await db.classes.create_index("teacher_id")
    await db.attendance.create_index("student_id")
    await db.attendance.create_index("class_id")
    # Enforce unique attendance per student per QR code (session)
    # --- ensure unique compound index on attendance (dedupe first) ---
    try:
        logger.info("Checking attendance collection for duplicate (student_id, qr_code_id) pairs...")
        pipeline = [
            {
                "$group": {
                    "_id": {"student_id": "$student_id", "qr_code_id": "$qr_code_id"},
                    "ids": {"$push": "$_id"},
                    "count": {"$sum": 1},
                }
            },
            {"$match": {"count": {"$gt": 1}}},
        ]

        duplicate_groups = await db.attendance.aggregate(pipeline).to_list(length=None)

        ids_to_delete = []
        for grp in duplicate_groups:
            ids = grp.get("ids", [])
            if not ids or len(ids) <= 1:
                continue
            # keep the earliest _id by sorting
            ids_sorted = sorted(ids, key=lambda x: ObjectId(x) if not isinstance(x, ObjectId) else x)
            keep_id = ids_sorted[0]
            remove_ids = ids_sorted[1:]
            logger.info(f"Duplicate group for {grp['_id']}: keep {keep_id}, delete {len(remove_ids)} duplicates")
            ids_to_delete.extend(remove_ids)

        if ids_to_delete:
            delete_result = await db.attendance.delete_many({"_id": {"$in": ids_to_delete}})
            logger.info(f"Deleted {delete_result.deleted_count} duplicate attendance documents.")
        else:
            logger.info("No duplicate attendance documents found.")

        try:
            await db.attendance.create_index([("student_id", 1), ("qr_code_id", 1)], unique=True)
            logger.info("Created unique index on attendance(student_id, qr_code_id).")
        except Exception as e_index:
            logger.error("Could not create unique index on attendance (startup). Index not created: %s", e_index)

    except Exception as e:
        logger.exception("Error during attendance dedupe/index creation: %s", e)

    await db.qr_codes.create_index("id", unique=True)
    await db.qr_codes.create_index("expires_at", expireAfterSeconds=0)
    logger.info("MongoDB indexes created.")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
