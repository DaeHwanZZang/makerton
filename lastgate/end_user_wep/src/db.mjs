import { DatabaseSync } from 'node:sqlite';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

function createPickupCode(db) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const code = String(crypto.randomInt(100000, 1000000));
    if (!db.prepare('SELECT 1 FROM parcels WHERE pickup_code = ?').get(code)) return code;
  }
  throw new Error('Could not allocate a unique pickup code');
}

export function openDatabase(file = ':memory:') {
  if (file !== ':memory:') fs.mkdirSync(path.dirname(path.resolve(file)), { recursive: true });
  const db = new DatabaseSync(file);
  db.exec('PRAGMA foreign_keys = ON');
  if (file !== ':memory:') db.exec('PRAGMA journal_mode = WAL');
  db.exec(`
    CREATE TABLE IF NOT EXISTS parcels (
      id TEXT PRIMARY KEY,
      request_id TEXT NOT NULL UNIQUE,
      tracking_number TEXT,
      building TEXT NOT NULL,
      unit TEXT NOT NULL,
      size TEXT NOT NULL CHECK (size IN ('S','M','L')),
      weight_kg REAL,
      sort_zone TEXT NOT NULL,
      status TEXT NOT NULL,
      locker_id TEXT,
      pickup_code TEXT,
      track_token TEXT NOT NULL UNIQUE,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS parcel_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      parcel_id TEXT NOT NULL REFERENCES parcels(id),
      from_status TEXT,
      to_status TEXT NOT NULL,
      actor TEXT NOT NULL,
      note TEXT,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS users (
      id TEXT PRIMARY KEY,
      email TEXT NOT NULL UNIQUE,
      password_hash TEXT NOT NULL,
      name TEXT NOT NULL,
      building TEXT NOT NULL,
      unit TEXT NOT NULL,
      default_delivery TEXT NOT NULL DEFAULT 'ROBOT'
        CHECK (default_delivery IN ('UNDECIDED','PICKUP','ROBOT')),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS user_sessions (
      token_hash TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      expires_at TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS parcel_delivery_choices (
      parcel_id TEXT PRIMARY KEY REFERENCES parcels(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      method TEXT NOT NULL CHECK (method IN ('PICKUP','ROBOT')),
      robot_id TEXT,
      requested_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS user_hidden_parcels (
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      parcel_id TEXT NOT NULL REFERENCES parcels(id) ON DELETE CASCADE,
      hidden_at TEXT NOT NULL,
      PRIMARY KEY (user_id, parcel_id)
    );
    CREATE TABLE IF NOT EXISTS robot_telemetry (
      robot_id TEXT PRIMARY KEY,
      connection_mode TEXT NOT NULL CHECK (connection_mode IN ('SIMULATED','MQTT','OFFLINE')),
      available INTEGER NOT NULL DEFAULT 1,
      status TEXT NOT NULL,
      location TEXT NOT NULL,
      battery INTEGER NOT NULL,
      user_visible INTEGER NOT NULL DEFAULT 0 CHECK (user_visible IN (0,1)),
      last_seen TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_parcels_status ON parcels(status);
    CREATE INDEX IF NOT EXISTS idx_parcels_created_at ON parcels(created_at);
    CREATE INDEX IF NOT EXISTS idx_parcels_address ON parcels(building, unit, created_at);
    CREATE INDEX IF NOT EXISTS idx_events_parcel_id ON parcel_events(parcel_id);
    CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON user_sessions(user_id);
  `);

  // Existing SQLite files are migrated in place; no parcel rows are recreated.
  const columns = db.prepare('PRAGMA table_info(parcels)').all();
  if (!columns.some(column => column.name === 'pickup_code')) {
    db.exec('ALTER TABLE parcels ADD COLUMN pickup_code TEXT');
  }
  const missingCodes = db.prepare('SELECT id FROM parcels WHERE pickup_code IS NULL').all();
  const setPickupCode = db.prepare('UPDATE parcels SET pickup_code = ? WHERE id = ?');
  for (const row of missingCodes) setPickupCode.run(createPickupCode(db), row.id);
  db.exec('CREATE UNIQUE INDEX IF NOT EXISTS idx_parcels_pickup_code ON parcels(pickup_code)');
  const userColumns = db.prepare('PRAGMA table_info(users)').all();
  if (!userColumns.some(column => column.name === 'default_delivery')) {
    db.exec("ALTER TABLE users ADD COLUMN default_delivery TEXT NOT NULL DEFAULT 'ROBOT'");
  }
  if (!userColumns.some(column => column.name === 'updated_at')) {
    db.exec('ALTER TABLE users ADD COLUMN updated_at TEXT');
  }
  const migrationNow = new Date().toISOString();
  db.prepare("UPDATE users SET default_delivery = 'ROBOT' WHERE default_delivery IS NULL OR default_delivery = 'UNDECIDED'").run();
  db.prepare('UPDATE users SET updated_at = COALESCE(updated_at, created_at, ?) WHERE updated_at IS NULL').run(migrationNow);
  const robotColumns = db.prepare('PRAGMA table_info(robot_telemetry)').all();
  if (!robotColumns.some(column => column.name === 'user_visible')) {
    db.exec('ALTER TABLE robot_telemetry ADD COLUMN user_visible INTEGER NOT NULL DEFAULT 0');
  }
  // 택배 등록은 보관소 입고를 뜻한다. 기존 접수/분류 건도 최초 한 번 전환한다.
  const legacyParcels = db.prepare("SELECT id, status FROM parcels WHERE status IN ('RECEIVED', 'SORTING')").all();
  if (legacyParcels.length) {
    const now = new Date().toISOString();
    db.exec('BEGIN IMMEDIATE');
    try {
      const update = db.prepare("UPDATE parcels SET status = 'STORED', updated_at = ? WHERE id = ?");
      const event = db.prepare(`INSERT INTO parcel_events
        (parcel_id, from_status, to_status, actor, note, created_at)
        VALUES (?, ?, 'STORED', 'migration', '기존 접수 상태를 보관 완료로 전환', ?)`);
      for (const parcel of legacyParcels) {
        update.run(now, parcel.id);
        event.run(parcel.id, parcel.status, now);
      }
      db.exec('COMMIT');
    } catch (error) {
      db.exec('ROLLBACK');
      throw error;
    }
  }
  return db;
}
