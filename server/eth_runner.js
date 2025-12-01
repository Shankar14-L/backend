#!/usr/bin/env node

/**
 * Ethereum Runner Script
 * Handles blockchain interactions for the attendance system
 * Usage: node eth_runner.js <action> <json_payload>
 *
 * Changes made:
 * - Use CONFIG.RPC_URL everywhere (avoid mixing SEPOLIA_RPC_URL).
 * - Delay creating provider/wallet until getContract() to avoid undefined providers.
 * - Add defensive checks and try/catch around on-chain operations.
 * - Diagnostics on stderr; final JSON on stdout (so callers can json.loads safely).
 */

const { ethers } = require('ethers');
const fs = require('fs');
const path = require('path');
require("dotenv").config();

// Configuration
const CONFIG = {
    RPC_URL: process.env.ETH_RPC_URL || process.env.RPC_URL || 'http://127.0.0.1:8545', // primary RPC
    PRIVATE_KEY: process.env.PRIVATE_KEY || '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80', // Hardhat default
    CONTRACT_ADDRESS: process.env.CONTRACT_ADDRESS || null,
    GAS_LIMIT: process.env.GAS_LIMIT ? Number(process.env.GAS_LIMIT) : 7000000
};

// helper: sanitize address strings from weird env formatting
function sanitizeAddress(raw) {
  if (!raw) return "";
  if (raw.includes("=") && raw.includes("0x")) raw = raw.slice(raw.indexOf("0x"));
  return raw.trim().replace(/^"+|"+$/g, "").replace(/^'+|'+$/g, "");
}

// Attempt to read CONTRACT_ADDRESS from env (sanitized) or from CONFIG
const SANITIZED_ENV_CONTRACT = sanitizeAddress(process.env.CONTRACT_ADDRESS);
const EFFECTIVE_CONTRACT_ADDR = SANITIZED_ENV_CONTRACT || CONFIG.CONTRACT_ADDRESS || null;

// Diagnostics to stderr only
console.error("NODE: CONFIG.RPC_URL:", CONFIG.RPC_URL);
console.error("NODE: Effective contract address (env/deployment):", EFFECTIVE_CONTRACT_ADDR);
console.error("NODE: Using PRIVATE_KEY set:", !!process.env.PRIVATE_KEY);

/**
 * Contract ABI (minimal, for interaction)
 */
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
 * - reads deployment.json if necessary
 * - constructs provider and wallet only here
 */
async function getContract() {
    try {
        // helpful debug about ethers shape/version
        try {
            // some versions expose `version`, others may expose util info
            const ev = (ethers && (ethers.version || (ethers.utils && ethers.utils.version))) || 'unknown';
            console.error("NODE: ethers version/identifier:", ev);
        } catch (e) {
            console.error("NODE: unable to read ethers.version:", e && e.message ? e.message : e);
        }

        // Determine contract address (priority: sanitized env -> CONFIG -> deployment.json)
        let contractAddress = EFFECTIVE_CONTRACT_ADDR || CONFIG.CONTRACT_ADDRESS;

        if (!contractAddress) {
            const deploymentPath = path.join(__dirname, 'deployment.json');
            if (fs.existsSync(deploymentPath)) {
                try {
                    const deployment = JSON.parse(fs.readFileSync(deploymentPath, 'utf8'));
                    contractAddress = deployment.contractAddress || deployment.address || contractAddress;
                    console.error("NODE: loaded contractAddress from deployment.json:", contractAddress);
                } catch (e) {
                    console.error("NODE: failed to parse deployment.json:", e && e.message ? e.message : e);
                }
            }
        }

        if (!contractAddress) {
            throw new Error('Contract address not found. Please set CONTRACT_ADDRESS env or provide deployment.json.');
        }

        // Create provider in a way that works for both ethers v5 and v6 (and other shapes)
        let provider;
        try {
            if (ethers && ethers.providers && ethers.providers.JsonRpcProvider) {
                console.error("NODE: using ethers.providers.JsonRpcProvider (v5 style)");
                provider = new ethers.providers.JsonRpcProvider(CONFIG.RPC_URL);
            } else if (ethers && ethers.JsonRpcProvider) {
                console.error("NODE: using ethers.JsonRpcProvider (v6/top-level style)");
                provider = new ethers.JsonRpcProvider(CONFIG.RPC_URL);
            } else if (ethers && typeof ethers.getDefaultProvider === 'function') {
                console.error("NODE: using ethers.getDefaultProvider as fallback");
                // getDefaultProvider accepts a network or options; passing RPC_URL will attempt to use it as a network param,
                // but it's the best effort fallback — many environments will have v5/v6 providers available.
                provider = ethers.getDefaultProvider(CONFIG.RPC_URL);
            } else {
                throw new Error('No usable JsonRpcProvider found on ethers import.');
            }
        } catch (provErr) {
            console.error("NODE: provider construction failed:", provErr && provErr.message ? provErr.message : provErr);
            throw provErr;
        }

        // Wallet construction — v5 and v6 both support new ethers.Wallet(privateKey, provider)
        let wallet;
        try {
            wallet = new ethers.Wallet(CONFIG.PRIVATE_KEY, provider);
        } catch (wErr) {
            console.error("NODE: wallet construction failed:", wErr && wErr.message ? wErr.message : wErr);
            throw wErr;
        }

        console.error("NODE: provider ready, wallet address:", wallet.address);

        // Create contract instance connected to wallet
        const contract = new ethers.Contract(contractAddress, CONTRACT_ABI, wallet);
        if (!contract || typeof contract.createSession !== 'function') {
            // defensive message with ABI shape details
            console.error("NODE: contract instance missing createSession method — check ABI and address.");
            throw new Error('Contract instance invalid or missing expected methods. Check ABI and contract address.');
        }

        return { contract, provider, wallet };
    } catch (error) {
        // rethrow with context (this will be caught in main())
        throw new Error(`Failed to initialize contract: ${error && error.message ? error.message : error}`);
    }
}

/**
 * Create attendance session (safe wrapper)
 * Note: this returns tx.hash immediately (does NOT wait for mining) to avoid long HTTP waits.
 * If you want to wait for mining, implement a separate background worker or change here intentionally.
 */
async function createSession(payload) {
    const { sessionCode, classId, durationMinutes = 30 } = payload;
    
    if (!sessionCode || !classId) {
        throw new Error('Missing required fields: sessionCode, classId');
    }

    const { contract } = await getContract();

    if (!contract || typeof contract.createSession !== 'function') {
        throw new Error('Contract not initialized or does not implement createSession');
    }

    try {
        console.error("NODE: createSession -> calling contract.createSession", { sessionCode, classId, durationMinutes });
        // submit tx and return tx.hash immediately — avoids waiting for mining in HTTP request
        const tx = await contract.createSession(sessionCode, classId, durationMinutes, { gasLimit: CONFIG.GAS_LIMIT });
        console.error("NODE: createSession -> tx submitted:", tx.hash);
        return {
            success: true,
            txHash: tx.hash,
            sessionCode,
            classId,
            message: "Transaction submitted (not awaited); confirm status asynchronously."
        };
    } catch (error) {
        // log detailed error to stderr for debugging
        console.error("NODE: createSession error:", error && error.stack ? error.stack : error);
        // throw a structured error up so main() returns JSON to stdout that can be parsed
        throw new Error(`createSession failed: ${error && error.message ? error.message : error}`);
    }
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
    if (!contract || typeof contract.markAttendance !== 'function') {
        throw new Error('Contract not initialized or does not implement markAttendance');
    }

    try {
        console.error("NODE: markAttendance -> calling contract.markAttendance", { sessionCode, studentId, classId });
        const tx = await contract.markAttendance(sessionCode, studentId, classId, { gasLimit: CONFIG.GAS_LIMIT });
        console.error("NODE: markAttendance -> tx submitted:", tx.hash);
        return {
            success: true,
            txHash: tx.hash,
            blockNumber: null,
            sessionCode,
            studentId,
            timestamp: Date.now(),
            message: "Transaction submitted (not awaited)"
        };
    } catch (error) {
        console.error("NODE: markAttendance error:", error && error.stack ? error.stack : error);
        throw new Error(`markAttendance failed: ${error && error.message ? error.message : error}`);
    }
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
    if (!contract || typeof contract.isSessionValid !== 'function') {
        throw new Error('Contract not initialized or does not implement isSessionValid');
    }
    try {
        const valid = await contract.isSessionValid(sessionCode);
        return {
            success: true,
            sessionCode,
            isValid: valid
        };
    } catch (error) {
        console.error("NODE: isSessionValid error:", error && error.stack ? error.stack : error);
        throw new Error(`isSessionValid failed: ${error && error.message ? error.message : error}`);
    }
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
    if (!contract || typeof contract.hasAttended !== 'function') {
        throw new Error('Contract not initialized or does not implement hasAttended');
    }

    try {
        const attended = await contract.hasAttended(sessionCode, studentId);
        return {
            success: true,
            sessionCode,
            studentId,
            hasAttended: attended
        };
    } catch (error) {
        console.error("NODE: hasAttended error:", error && error.stack ? error.stack : error);
        throw new Error(`hasAttended failed: ${error && error.message ? error.message : error}`);
    }
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
    if (!contract || typeof contract.getAttendanceRecord !== 'function') {
        throw new Error('Contract not initialized or does not implement getAttendanceRecord');
    }

    try {
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
    } catch (error) {
        console.error("NODE: getAttendanceRecord error:", error && error.stack ? error.stack : error);
        throw new Error(`getAttendanceRecord failed: ${error && error.message ? error.message : error}`);
    }
}

/**
 * Get total records count
 */
async function getTotalRecords() {
    const { contract } = await getContract();
    if (!contract || typeof contract.getTotalRecords !== 'function') {
        throw new Error('Contract not initialized or does not implement getTotalRecords');
    }
    try {
        const total = await contract.getTotalRecords();
        return {
            success: true,
            totalRecords: Number(total)
        };
    } catch (error) {
        console.error("NODE: getTotalRecords error:", error && error.stack ? error.stack : error);
        throw new Error(`getTotalRecords failed: ${error && error.message ? error.message : error}`);
    }
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
    if (!contract || typeof contract.getRecordByIndex !== 'function') {
        throw new Error('Contract not initialized or does not implement getRecordByIndex');
    }

    try {
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
    } catch (error) {
        console.error("NODE: getRecordByIndex error:", error && error.stack ? error.stack : error);
        throw new Error(`getRecordByIndex failed: ${error && error.message ? error.message : error}`);
    }
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
    if (!contract || typeof contract.authorizeTeacher !== 'function') {
        throw new Error('Contract not initialized or does not implement authorizeTeacher');
    }

    try {
        const tx = await contract.authorizeTeacher(teacherAddress, { gasLimit: CONFIG.GAS_LIMIT });
        console.error("NODE: authorizeTeacher tx submitted:", tx.hash);
        return {
            success: true,
            txHash: tx.hash,
            teacherAddress
        };
    } catch (error) {
        console.error("NODE: authorizeTeacher error:", error && error.stack ? error.stack : error);
        throw new Error(`authorizeTeacher failed: ${error && error.message ? error.message : error}`);
    }
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
    if (!contract || typeof contract.registerStudent !== 'function') {
        throw new Error('Contract not initialized or does not implement registerStudent');
    }

    try {
        const tx = await contract.registerStudent(studentId, studentAddress, { gasLimit: CONFIG.GAS_LIMIT });
        console.error("NODE: registerStudent tx submitted:", tx.hash);
        return {
            success: true,
            txHash: tx.hash,
            studentId,
            studentAddress
        };
    } catch (error) {
        console.error("NODE: registerStudent error:", error && error.stack ? error.stack : error);
        throw new Error(`registerStudent failed: ${error && error.message ? error.message : error}`);
    }
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
        // diagnostics on stderr (stack), structured error JSON on stdout for caller parsing
        console.error("NODE ERROR:", error && error.stack ? error.stack : error);
        const errObj = {
            success: false,
            error: error && error.message ? error.message : String(error),
            stack: error && error.stack ? error.stack : null
        };
        // ensure stdout has something parseable
        console.log(JSON.stringify(errObj));
        process.exit(1);
    }
}

// Run if called directly
if (require.main === module) {
    main();
}

// Export functions for programmatic use
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
