// Copyright (c) 2026 Leo Liang
// SPDX-License-Identifier: MIT

import { existsSync, readFileSync, realpathSync, statSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";
import process from "node:process";

const root = resolve(process.cwd());
const marketplacePath = resolve(root, ".cursor-plugin", "marketplace.json");
const errors = [];

function report(message) {
  errors.push(message);
}

function readJson(path, label) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    report(`${label} is not valid JSON: ${error.message}`);
    return null;
  }
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function requireString(object, field, label) {
  if (typeof object?.[field] !== "string" || object[field].trim() === "") {
    report(`${label}.${field} must be a non-empty string`);
    return null;
  }
  return object[field];
}

function inside(base, candidate) {
  const path = relative(base, candidate);
  return path === "" || (!path.startsWith("..") && !isAbsolute(path));
}

function resolveInside(base, value, label) {
  if (isAbsolute(value)) {
    report(`${label} must be relative to its payload`);
    return null;
  }
  const path = resolve(base, value);
  if (!inside(base, path)) {
    report(`${label} must stay inside its payload`);
    return null;
  }
  return path;
}

function requirePath(payload, manifest, field, kind) {
  const value = requireString(manifest, field, "plugin manifest");
  if (value === null) return;
  const path = resolveInside(payload, value, `plugin manifest.${field}`);
  if (path === null) return;
  if (!existsSync(path)) {
    report(`plugin manifest.${field} does not exist: ${value}`);
    return;
  }
  if (!inside(realpathSync(payload), realpathSync(path))) {
    report(`plugin manifest.${field} must not resolve outside its payload: ${value}`);
    return;
  }
  if (kind === "directory" && !statSync(path).isDirectory()) {
    report(`plugin manifest.${field} must name a directory: ${value}`);
  }
  if (kind === "file" && !statSync(path).isFile()) {
    report(`plugin manifest.${field} must name a file: ${value}`);
  }
}

function validatePlugin(entry, index) {
  const label = `marketplace.plugins[${index}]`;
  if (!isObject(entry)) {
    report(`${label} must be an object`);
    return;
  }
  const name = requireString(entry, "name", label);
  const source = requireString(entry, "source", label);
  requireString(entry, "description", label);
  if (source === null) return;

  const payload = resolveInside(root, source, `${label}.source`);
  if (payload === null) return;
  if (!existsSync(payload) || !statSync(payload).isDirectory()) {
    report(`${label}.source must name an existing payload directory: ${source}`);
    return;
  }
  if (!inside(realpathSync(root), realpathSync(payload))) {
    report(`${label}.source must not resolve outside the repository`);
    return;
  }

  const manifestPath = resolve(payload, ".cursor-plugin", "plugin.json");
  if (!inside(payload, manifestPath) || !existsSync(manifestPath)) {
    report(`${label}.source is missing .cursor-plugin/plugin.json`);
    return;
  }
  if (!inside(realpathSync(payload), realpathSync(manifestPath))) {
    report(`${label}.source manifest must not resolve outside its payload`);
    return;
  }
  const manifest = readJson(manifestPath, `${label} plugin manifest`);
  if (!isObject(manifest)) return;

  const manifestName = requireString(manifest, "name", "plugin manifest");
  requireString(manifest, "displayName", "plugin manifest");
  requireString(manifest, "description", "plugin manifest");
  const version = requireString(manifest, "version", "plugin manifest");
  if (!isObject(manifest.author)) {
    report("plugin manifest.author must be an object");
  } else {
    requireString(manifest.author, "name", "plugin manifest.author");
  }
  if (name !== null && manifestName !== null && name !== manifestName) {
    report(`${label}.name (${name}) does not match plugin manifest.name (${manifestName})`);
  }

  requirePath(payload, manifest, "skills", "directory");
  requirePath(payload, manifest, "agents", "directory");
  requirePath(payload, manifest, "hooks", "file");

  const packagePath = resolve(payload, "package.json");
  if (!existsSync(packagePath)) {
    report(`${label}.source is missing package.json for version alignment`);
    return;
  }
  if (!inside(realpathSync(payload), realpathSync(packagePath))) {
    report(`${label}.source package.json must not resolve outside its payload`);
    return;
  }
  const packageJson = readJson(packagePath, `${label} package.json`);
  if (!isObject(packageJson)) return;
  const packageVersion = requireString(packageJson, "version", "package.json");
  if (version !== null && packageVersion !== null && version !== packageVersion) {
    report(`plugin manifest.version (${version}) does not match package.json.version (${packageVersion})`);
  }
  if (typeof entry.version === "string" && version !== null && entry.version !== version) {
    report(`${label}.version (${entry.version}) does not match plugin manifest.version (${version})`);
  }
}

if (!existsSync(marketplacePath)) {
  report(`missing marketplace manifest: ${relative(root, marketplacePath)}`);
} else {
  const marketplace = readJson(marketplacePath, "marketplace manifest");
  if (isObject(marketplace)) {
    requireString(marketplace, "name", "marketplace manifest");
    if (!isObject(marketplace.owner)) {
      report("marketplace manifest.owner must be an object");
    } else {
      requireString(marketplace.owner, "name", "marketplace manifest.owner");
    }
    if (!isObject(marketplace.metadata)) {
      report("marketplace manifest.metadata must be an object");
    } else {
      requireString(marketplace.metadata, "description", "marketplace manifest.metadata");
    }
    if (!Array.isArray(marketplace.plugins) || marketplace.plugins.length === 0) {
      report("marketplace manifest.plugins must be a non-empty array");
    } else {
      marketplace.plugins.forEach(validatePlugin);
    }
  } else if (marketplace !== null) {
    report("marketplace manifest must be an object");
  }
}

if (errors.length > 0) {
  for (const error of errors) console.error(`error: ${error}`);
  process.exitCode = 1;
} else {
  console.log(`Cursor plugin validation passed: ${relative(root, marketplacePath)}`);
}
