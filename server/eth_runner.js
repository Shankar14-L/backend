#!/usr/bin/env node

/**
 * Ethereum Runner Script
 * Handles blockchain interactions for the attendance system
 * Usage: node eth_runner.js <action> <json_payload>
 *
 * NOTE: diagnostics/logs are intentionally written to stderr so callers (like Python)
 * can safely parse JSON printed to stdout.
 */

const { ethers } = require('ethers');
const fs = require('fs');
const path = require('path');
require("dotenv").config();
// Configuration
const CONFIG = {
    RPC_URL: process.env.ETH_RPC_URL || 'http://127.0.0.1:8545', // Local Hardhat/Ganache
    PRIVATE_KEY: process.env.PRIVATE_KEY || '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80', // Hardhat default
    CONTRACT_ADDRESS: process.env.CONTRACT_ADDRESS || null,
    GAS_LIMIT: process.env.GAS_LIMIT || 7000000
};
// === BEGIN: robust eth_runner helper ===

function sanitizeAddress(raw) {
  if (!raw) return "";
  if (raw.includes("=") && raw.includes("0x")) raw = raw.slice(raw.indexOf("0x"));
  return raw.trim().replace(/^"+|"+$/g, "").replace(/^'+|'+$/g, "");
}

const CONTRACT_ADDRESS = sanitizeAddress(process.env.CONTRACT_ADDRESS);
if (!CONTRACT_ADDRESS) {
  // do not throw here — getContract handles missing address later; but warn we don't have env var
  console.error("NODE: CONTRACT_ADDRESS env not set or invalid (sanitized empty). Will attempt deployment.json.");
}
const provider = new (ethers.providers && ethers.providers.JsonRpcProvider ? ethers.providers.JsonRpcProvider : (ethers.JsonRpcProvider || ethers.getDefaultProvider))(process.env.SEPOLIA_RPC_URL);
const signer = new ethers.Wallet(process.env.PRIVATE_KEY, provider);

// diagnostics go to stderr so stdout remains JSON for programmatic callers
console.error("NODE: Using CONTRACT_ADDRESS:", CONTRACT_ADDRESS || CONFIG.CONTRACT_ADDRESS);
console.error("NODE: Using signer addr:", signer.address);

async function safeCreateSession(contract, sessionCode, classId, durationMinutes) {
  // diagnostic on stderr
  const balance = await signer.getBalance();
  console.error("NODE: signer balance (wei):", balance.toString(), "ETH:", Number(balance)/1e18);

  // estimate gas
  let gasEstimate;
  try {
    gasEstimate = await contract.estimateGas.createSession(sessionCode, classId, durationMinutes, { from: signer.address });
    console.error("NODE: gasEstimate:", gasEstimate.toString());
  } catch (e) {
    console.error("NODE: estimateGas failed:", e.message || e);
  }

  const feeData = await provider.getFeeData();
  const defaultPriority = ethers.parseUnits ? ethers.parseUnits("1", "gwei") : (ethers.utils && ethers.utils.parseUnits ? ethers.utils.parseUnits("1", "gwei") : "1000000000");
  const defaultMax = ethers.parseUnits ? ethers.parseUnits("2", "gwei") : (ethers.utils && ethers.utils.parseUnits ? ethers.utils.parseUnits("2", "gwei") : "2000000000");
  const maxPriority = feeData.maxPriorityFeePerGas ?? defaultPriority;
  const maxFee = feeData.maxFeePerGas ?? defaultMax;
  const gasLimit = gasEstimate ? (gasEstimate * 12n / 10n) : 200000n;
  const required = gasLimit * maxFee;
  console.error("NODE: feeData:", {
    maxFee: typeof maxFee === "bigint" ? maxFee.toString() : maxFee,
    maxPriority: typeof maxPriority === "bigint" ? maxPriority.toString() : maxPriority,
    gasLimit: gasLimit.toString(),
    requiredWei: typeof required === "bigint" ? required.toString() : required,
    requiredEth: (typeof required === "bigint") ? Number(required)/1e18 : (Number(required) / 1e18)
  });

  if (balance < required) {
    throw new Error(`Insufficient funds: balance ${balance.toString()} < required ${required.toString()}`);
  }

  const tx = await contract.connect(signer).createSession(sessionCode, classId, durationMinutes, {
    gasLimit,
    maxPriorityFeePerGas: maxPriority,
    maxFeePerGas: maxFee
  });
  console.error("NODE: tx hash", tx.hash);
  const receipt = await tx.wait();
  console.error("NODE: tx mined in block", receipt.blockNumber);
  return receipt;
}
// === END helper ===

module.exports = { safeCreateSession, CONTRACT_ADDRESS, provider, signer };

// Contract ABI (minimal, for interaction)
const CONTRACT_ABI = [
    "function createSession(string sessionCode, string classId, uint256 durationMinutes) external",
    "function markAttendance(string sessionCode, string studentId, string classId) external",
    "function hasAttended(string sessionCode, string studentId) external view returns (bool)",
    "function getAttendanceRecord(string sessionCode, string studentId) external view returns (tuple(string sessionCode, string classId, string studentId, address studentAddress, uint256 timestamp, bool verified))",
    "function isSessionValid(string sessionCode) external view returns (bool)",
    "function getTotalRecords() external view returns (uint256)",
    "function getRecordByIndex(uint256 index) external view returns (tuple(string sessionCode, string classId, string studentId, address studentAddress, uint256 timestamp, bool verified))",
    "function authorizeTeacher(address teacher) external",
    "function registerStudent(string studentId, address studentAddress) external"
];

/**
 * Get contract instance
 */
async function getContract() {
    try {
        // Read contract address from deployment file if not in env
        let contractAddress = CONFIG.CONTRACT_ADDRESS;
        
        if (!contractAddress) {
            const deploymentPath = path.join(__dirname, 'deployment.json');
            if (fs.existsSync(deploymentPath)) {
                const deployment = JSON.parse(fs.readFileSync(deploymentPath, 'utf8'));
                contractAddress = deployment.contractAddress;
                console.error("NODE: loaded contractAddress from deployment.json:", contractAddress);
            }
        }

        if (!contractAddress) {
            throw new Error('Contract address not found. Please deploy contract first.');
        }

        // Setup provider and signer
        const provider = new ethers.JsonRpcProvider(CONFIG.RPC_URL);
        const wallet = new ethers.Wallet(CONFIG.PRIVATE_KEY, provider);
        
        // Create contract instance
        const contract = new ethers.Contract(contractAddress, CONTRACT_ABI, wallet);
        
        return { contract, provider, wallet };
    } catch (error) {
        throw new Error(`Failed to initialize contract: ${error.message}`);
    }
}

/**
 * Create attendance session
 */
async function createSession(payload) {
    const { sessionCode, classId, durationMinutes = 30 } = payload;
    
    if (!sessionCode || !classId) {
        throw new Error('Missing required fields: sessionCode, classId');
    }

    const { contract } = await getContract();
    
    const tx = await contract.createSession(
        sessionCode,
        classId,
        durationMinutes,
        { gasLimit: CONFIG.GAS_LIMIT }
    );
    
    const receipt = await tx.wait();
    
    return {
        success: true,
        txHash: receipt.hash,
        blockNumber: receipt.blockNumber,
        sessionCode,
        classId
    };
}

/**
 * Mark attendance
 */
async function markAttendance(payload) {
    const { sessionCode, studentId, classId } = payload;
    
    if (!sessionCode || !studentId || !classId) {
        throw new Error('Missing required fields: sessionCode, studentId, classId');
    }

    const { contract } = await getContract();
    
    // Check if session is valid
    const isValid = await contract.isSessionValid(sessionCode);
    if (!isValid) {
        throw new Error('Session is invalid or expired');
    }
    
    // Check if already attended
    const hasAttended = await contract.hasAttended(sessionCode, studentId);
    if (hasAttended) {
        throw new Error('Attendance already marked for this session');
    }
    
    const tx = await contract.markAttendance(
        sessionCode,
        studentId,
        classId,
        { gasLimit: CONFIG.GAS_LIMIT }
    );
    
    const receipt = await tx.wait();
    
    return {
        success: true,
        txHash: receipt.hash,
        blockNumber: receipt.blockNumber,
        sessionCode,
        studentId,
        timestamp: Date.now()
    };
}

/**
 * Check if session is valid
 */
async function isSessionValid(payload) {
    const { sessionCode } = payload;
    if (!sessionCode) {
        throw new Error('Missing required field: sessionCode');
    }
    const { contract } = await getContract();
    const valid = await contract.isSessionValid(sessionCode);
    return {
        success: true,
        sessionCode,
        isValid: valid
    };
}

/**
 * Check if student attended
 */
async function hasAttended(payload) {
    const { sessionCode, studentId } = payload;
    
    if (!sessionCode || !studentId) {
        throw new Error('Missing required fields: sessionCode, studentId');
    }

    const { contract } = await getContract();
    const attended = await contract.hasAttended(sessionCode, studentId);
    
    return {
        success: true,
        sessionCode,
        studentId,
        hasAttended: attended
    };
}

/**
 * Get attendance record
 */
async function getAttendanceRecord(payload) {
    const { sessionCode, studentId } = payload;
    
    if (!sessionCode || !studentId) {
        throw new Error('Missing required fields: sessionCode, studentId');
    }

    const { contract } = await getContract();
    const record = await contract.getAttendanceRecord(sessionCode, studentId);
    
    return {
        success: true,
        record: {
            sessionCode: record.sessionCode,
            classId: record.classId,
            studentId: record.studentId,
            studentAddress: record.studentAddress,
            timestamp: Number(record.timestamp),
            verified: record.verified
        }
    };
}

/**
 * Get total records count
 */
async function getTotalRecords() {
    const { contract } = await getContract();
    const total = await contract.getTotalRecords();
    
    return {
        success: true,
        totalRecords: Number(total)
    };
}

/**
 * Get record by index
 */
async function getRecordByIndex(payload) {
    const { index } = payload;
    
    if (index === undefined) {
        throw new Error('Missing required field: index');
    }

    const { contract } = await getContract();
    const record = await contract.getRecordByIndex(index);
    
    return {
        success: true,
        record: {
            sessionCode: record.sessionCode,
            classId: record.classId,
            studentId: record.studentId,
            studentAddress: record.studentAddress,
            timestamp: Number(record.timestamp),
            verified: record.verified
        }
    };
}

/**
 * Authorize teacher
 */
async function authorizeTeacher(payload) {
    const { teacherAddress } = payload;
    
    if (!teacherAddress) {
        throw new Error('Missing required field: teacherAddress');
    }

    const { contract } = await getContract();
    
    const tx = await contract.authorizeTeacher(
        teacherAddress,
        { gasLimit: CONFIG.GAS_LIMIT }
    );
    
    const receipt = await tx.wait();
    
    return {
        success: true,
        txHash: receipt.hash,
        teacherAddress
    };
}

/**
 * Register student
 */
async function registerStudent(payload) {
    const { studentId, studentAddress } = payload;
    
    if (!studentId || !studentAddress) {
        throw new Error('Missing required fields: studentId, studentAddress');
    }

    const { contract } = await getContract();
    
    const tx = await contract.registerStudent(
        studentId,
        studentAddress,
        { gasLimit: CONFIG.GAS_LIMIT }
    );
    
    const receipt = await tx.wait();
    
    return {
        success: true,
        txHash: receipt.hash,
        studentId,
        studentAddress
    };
}

/**
 * Main execution
 */
async function main() {
    try {
        const args = process.argv.slice(2);
        
        if (args.length < 1) {
            throw new Error('Usage: node eth_runner.js <action> <json_payload>');
        }

        const action = args[0];
        const payload = args[1] ? JSON.parse(args[1]) : {};

        let result;

        switch (action) {
            case 'createSession':
                result = await createSession(payload);
                break;
            case 'markAttendance':
                result = await markAttendance(payload);
                break;
            case 'isSessionValid':
                result = await isSessionValid(payload);
                break;
            case 'hasAttended':
                result = await hasAttended(payload);
                break;
            case 'getAttendanceRecord':
                result = await getAttendanceRecord(payload);
                break;
            case 'getTotalRecords':
                result = await getTotalRecords();
                break;
            case 'getRecordByIndex':
                result = await getRecordByIndex(payload);
                break;
            case 'authorizeTeacher':
                result = await authorizeTeacher(payload);
                break;
            case 'registerStudent':
                result = await registerStudent(payload);
                break;
            default:
                throw new Error(`Unknown action: ${action}`);
        }

        // final result must be JSON to stdout for caller
        console.log(JSON.stringify(result));
        process.exit(0);
    } catch (error) {
        // error details to stdout as JSON (so caller can parse error) and diagnostics to stderr
        console.error("NODE ERROR:", error && error.stack ? error.stack : error);
        console.log(JSON.stringify({
            success: false,
            error: error.message,
            stack: error.stack
        }));
        process.exit(1);
    }
}

// Run if called directly
if (require.main === module) {
    main();
}

module.exports = {
    createSession,
    markAttendance,
    isSessionValid,
    hasAttended,
    getAttendanceRecord,
    getTotalRecords,
    getRecordByIndex,
    authorizeTeacher,
    registerStudent
};
