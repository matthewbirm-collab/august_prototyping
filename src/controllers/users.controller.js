const User = require('../models/user.model');

function isValidEmail(email) {
  return typeof email === 'string' && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

function listUsers(req, res) {
  res.json(User.getAll());
}

function getUser(req, res) {
  const user = User.getById(Number(req.params.id));
  if (!user) return res.status(404).json({ error: 'User not found' });
  res.json(user);
}

function createUser(req, res) {
  const { name, email } = req.body;

  if (!name || typeof name !== 'string') {
    return res.status(400).json({ error: 'name is required and must be a string' });
  }
  if (!isValidEmail(email)) {
    return res.status(400).json({ error: 'A valid email is required' });
  }
  if (User.findByEmail(email)) {
    return res.status(409).json({ error: 'A user with this email already exists' });
  }

  const user = User.create({ name, email });
  res.status(201).json(user);
}

function updateUser(req, res) {
  const id = Number(req.params.id);
  const { name, email } = req.body;

  if (!User.getById(id)) {
    return res.status(404).json({ error: 'User not found' });
  }
  if (name !== undefined && typeof name !== 'string') {
    return res.status(400).json({ error: 'name must be a string' });
  }
  if (email !== undefined) {
    if (!isValidEmail(email)) {
      return res.status(400).json({ error: 'email must be a valid email address' });
    }
    const existing = User.findByEmail(email);
    if (existing && existing.id !== id) {
      return res.status(409).json({ error: 'A user with this email already exists' });
    }
  }

  const user = User.update(id, { name, email });
  res.json(user);
}

function deleteUser(req, res) {
  const id = Number(req.params.id);
  const deleted = User.remove(id);
  if (!deleted) return res.status(404).json({ error: 'User not found' });
  res.status(204).send();
}

module.exports = { listUsers, getUser, createUser, updateUser, deleteUser };
