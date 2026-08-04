let users = [];
let nextId = 1;

function reset() {
  users = [];
  nextId = 1;
}

function getAll() {
  return users;
}

function getById(id) {
  return users.find((user) => user.id === id);
}

function create({ name, email }) {
  const user = { id: nextId++, name, email };
  users.push(user);
  return user;
}

function update(id, { name, email }) {
  const user = getById(id);
  if (!user) return null;
  if (name !== undefined) user.name = name;
  if (email !== undefined) user.email = email;
  return user;
}

function remove(id) {
  const index = users.findIndex((user) => user.id === id);
  if (index === -1) return false;
  users.splice(index, 1);
  return true;
}

function findByEmail(email) {
  return users.find((user) => user.email === email);
}

module.exports = { getAll, getById, create, update, remove, findByEmail, reset };
