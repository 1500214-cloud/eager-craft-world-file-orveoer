"use strict";

const DATABASE_NAME = "_net_lax1dude_eaglercraft_v1_8_internal_PlatformFilesystem_1_12_2_";
const DATABASE_VERSION = 1;
const STORE_NAME = "filesystem";
const FORMAT_NAME = "eaglercraft-indexeddb-backup";
const BASE64_PATTERN = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
let pendingBackup = null;

function openDatabase(createIfMissing = false) {
  return new Promise((resolve, reject) => {
    let request;
    try {
      request = createIfMissing ? indexedDB.open(DATABASE_NAME, DATABASE_VERSION) : indexedDB.open(DATABASE_NAME);
    } catch (error) { reject(error); return; }
    request.onupgradeneeded = () => {
      if (!createIfMissing) { request.transaction.abort(); return; }
      if (!request.result.objectStoreNames.contains(STORE_NAME)) request.result.createObjectStore(STORE_NAME, { keyPath: ["path"] });
    };
    request.onsuccess = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(STORE_NAME)) { database.close(); reject(new Error("The Eaglercraft filesystem store was not found.")); return; }
      database.onversionchange = () => database.close();
      resolve(database);
    };
    request.onerror = () => reject(request.error || new Error("Could not open the Eaglercraft save database."));
  });
}

function getAllRecords(database) {
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(STORE_NAME, "readonly");
    const request = transaction.objectStore(STORE_NAME).getAll();
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("Could not read the filesystem records."));
    transaction.onabort = () => reject(transaction.error || new Error("Reading the filesystem was interrupted."));
  });
}

function dataByteLength(data) {
  if (data instanceof ArrayBuffer) return data.byteLength;
  if (ArrayBuffer.isView(data)) return data.byteLength;
  throw new Error("A filesystem record contains data in an unsupported format.");
}

async function summarizeDatabase() {
  self.postMessage({ type: "status", message: "Scanning saved worlds in the background…" });
  if (typeof indexedDB.databases === "function") {
    const databases = await indexedDB.databases();
    if (!databases.some((database) => database.name === DATABASE_NAME)) throw new Error("No Eaglercraft save database was found for this site.");
  }
  const database = await openDatabase();
  let records;
  try { records = await getAllRecords(database); }
  finally { database.close(); }
  const worlds = new Map();
  let worldFiles = 0;
  let totalBytes = 0;
  for (const value of records) {
    const match = typeof value.path === "string" && value.path.match(/^eaglercraft\/worlds\/([^/]+)\//);
    if (!match) continue;
    const size = dataByteLength(value.data);
    const world = worlds.get(match[1]) || { files: 0, bytes: 0 };
    world.files += 1; world.bytes += size; worlds.set(match[1], world);
    worldFiles += 1; totalBytes += size;
  }
  return { worlds: [...worlds.entries()], worldFiles, totalBytes, recordCount: records.length };
}

function bytesFromData(data) {
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  throw new Error("A filesystem record contains data in an unsupported format.");
}

function base64FromBytes(bytes) {
  let output = "";
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    output += String.fromCharCode(...bytes.subarray(offset, Math.min(offset + chunkSize, bytes.length)));
  }
  return btoa(output);
}

function bytesFromBase64(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes.buffer;
}

function base64ByteLength(base64) {
  const padding = base64.endsWith("==") ? 2 : base64.endsWith("=") ? 1 : 0;
  return base64.length * 3 / 4 - padding;
}

async function exportBackup() {
  self.postMessage({ type: "status", message: "Reading local files and encoding the backup…" });
  const database = await openDatabase();
  let records;
  try { records = await getAllRecords(database); }
  finally { database.close(); }
  const rows = records.map((value) => {
    const path = value.path;
    return { key: { 0: path }, value: { path, data: { type: "ArrayBuffer", base64: base64FromBytes(bytesFromData(value.data)) } } };
  });
  const backup = { format: FORMAT_NAME, database: DATABASE_NAME, version: DATABASE_VERSION, stores: { [STORE_NAME]: rows } };
  const blob = new Blob([JSON.stringify(backup)], { type: "application/json" });
  self.postMessage({ type: "exported", blob, recordCount: rows.length });
}

async function inspectBackup(file) {
  self.postMessage({ type: "status", message: "Reading and checking the snapshot in the background…" });
  let text = await file.text();
  const backup = JSON.parse(text);
  text = null;
  if (!backup || backup.format !== FORMAT_NAME) throw new Error("This is not an Eaglercraft IndexedDB backup.");
  if (backup.database !== DATABASE_NAME || backup.version !== DATABASE_VERSION) throw new Error("This backup is for a different Eaglercraft database or version.");
  const records = backup.stores?.[STORE_NAME];
  if (!Array.isArray(records) || records.length === 0) throw new Error("The backup has no filesystem records.");

  const seen = new Set();
  const worlds = new Map();
  for (const record of records) {
    const value = record?.value;
    const path = value?.path;
    const data = value?.data;
    if (typeof path !== "string" || !path || path.startsWith("/") || path.split("/").includes("..")) throw new Error("The backup contains an invalid filesystem path.");
    if (seen.has(path)) throw new Error("The backup contains duplicate filesystem paths.");
    seen.add(path);
    if (data?.type !== "ArrayBuffer" || typeof data.base64 !== "string" || !BASE64_PATTERN.test(data.base64)) throw new Error("The backup contains invalid file data.");
    const match = path.match(/^eaglercraft\/worlds\/([^/]+)\//);
    if (!match) continue;
    const world = worlds.get(match[1]) || { files: 0, bytes: 0 };
    world.files += 1; world.bytes += base64ByteLength(data.base64); worlds.set(match[1], world);
  }
  if (seen.size === 0) throw new Error("The backup has no filesystem records.");
  pendingBackup = backup;
  self.postMessage({ type: "validated", summary: { worldCount: worlds.size, recordCount: records.length } });
}

function bytesFromRecordData(data) {
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  if (data && typeof data.base64 === "string") return Uint8Array.from(atob(data.base64), character => character.charCodeAt(0));
  return null;
}

async function repairWorldList() {
  self.postMessage({ type: "status", message: "Scanning world folders and repairing the world list…" });
  const database = await openDatabase(false);
  let records;
  try {
    const transaction = database.transaction(STORE_NAME, "readonly");
    const request = transaction.objectStore(STORE_NAME).getAll();
    records = await new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("Could not read the filesystem records."));
    });
  } finally { database.close(); }

  const existingFolders = new Set();
  let priorText = "";
  for (const value of records) {
    if (typeof value.path !== "string") continue;
    if (value.path === "worlds_list.txt") {
      const bytes = bytesFromRecordData(value.data);
      if (bytes) priorText = new TextDecoder().decode(bytes);
      continue;
    }
    const match = value.path.match(/^eaglercraft\/worlds\/([^/]+)\/(.+)$/);
    if (match && match[2]) existingFolders.add(match[1]);
  }

  const priorNames = priorText.split(/\r?\n/).map(name => name.trim()).filter(Boolean);
  const repairedNames = [];
  const seen = new Set();
  for (const name of priorNames) {
    if (existingFolders.has(name) && !seen.has(name)) { repairedNames.push(name); seen.add(name); }
  }
  for (const name of [...existingFolders].sort((a, b) => a.localeCompare(b))) {
    if (!seen.has(name)) { repairedNames.push(name); seen.add(name); }
  }

  if (repairedNames.length === 0) throw new Error("No valid world folders were found to add to the world list.");

  const repairedBytes = new TextEncoder().encode(repairedNames.join("\n") + "\n");
  await openDatabase(true).then(database => new Promise((resolve, reject) => {
    let transaction;
    try {
      transaction = database.transaction(STORE_NAME, "readwrite");
      const store = transaction.objectStore(STORE_NAME);
      store.put({ path: "worlds_list.txt", data: repairedBytes.buffer });
    } catch (error) { database.close(); reject(error); return; }
    transaction.oncomplete = () => { database.close(); resolve(); };
    transaction.onerror = () => { database.close(); reject(transaction.error || new Error("Could not repair the world list.")); };
    transaction.onabort = () => { database.close(); reject(transaction.error || new Error("The world-list repair was interrupted.")); };
  }));

  const removed = priorNames.filter(name => !existingFolders.has(name));
  self.postMessage({ type: "repaired", repairedNames, removedNames: removed, folderCount: existingFolders.size });
}

function restoreBackup() {
  if (!pendingBackup) throw new Error("Select a valid backup before restoring.");
  self.postMessage({ type: "status", message: "Writing the snapshot into the local filesystem…" });
  const records = pendingBackup.stores[STORE_NAME];
  return openDatabase(true).then((database) => new Promise((resolve, reject) => {
    let transaction;
    try {
      transaction = database.transaction(STORE_NAME, "readwrite");
      const store = transaction.objectStore(STORE_NAME);
      store.clear();
      for (const record of records) store.put({ path: record.value.path, data: bytesFromBase64(record.value.data.base64) });
    } catch (error) {
      database.close(); reject(error); return;
    }
    transaction.oncomplete = () => { database.close(); resolve(); };
    transaction.onerror = () => { database.close(); reject(transaction.error || new Error("The restore transaction failed.")); };
    transaction.onabort = () => { database.close(); reject(transaction.error || new Error("The restore was interrupted.")); };
  })).then(() => { pendingBackup = null; self.postMessage({ type: "restored" }); });
}

self.addEventListener("message", async ({ data }) => {
  try {
    if (data.type === "export") await exportBackup();
    else if (data.type === "inspect") await inspectBackup(data.file);
    else if (data.type === "restore") await restoreBackup();
    else if (data.type === "summarize") self.postMessage({ type: "summarized", summary: await summarizeDatabase() });
  } catch (error) {
    self.postMessage({ type: "error", message: error.message || "The background filesystem task failed." });
  }
});