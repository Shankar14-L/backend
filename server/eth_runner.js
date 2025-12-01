#!/usr/bin/env node

/**
 * Ethereum Runner Script
 * Handles blockchain interactions for the attendance system
 * Usage: node eth_runner.js <action> <json_payload>
 */

const { ethers } = require('ethers');
const fs = require('fs');
const path = require('path');

// Configuration
const CONFIG = {
    RPC_URL: process.env.ETH_RPC_URL || 'http://127.0.0.1:8545', // Local Hardhat/Ganache
    PRIVATE_KEY: process.env.PRIVATE_KEY || '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80', // Hardhat default
    CONTRACT_ADDRESS: process.env.CONTRACT_ADDRESS || null,
    GAS_LIMIT: process.env.GAS_LIMIT || 7000000
};

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

        console.log(JSON.stringify(result));
        process.exit(0);
    } catch (error) {
        console.error(JSON.stringify({
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
