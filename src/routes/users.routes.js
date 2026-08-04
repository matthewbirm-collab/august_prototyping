const express = require('express');
const {
  listUsers,
  getUser,
  createUser,
  updateUser,
  deleteUser,
} = require('../controllers/users.controller');

const router = express.Router();

router.get('/', listUsers);
router.get('/:id', getUser);
router.post('/', createUser);
router.put('/:id', updateUser);
router.delete('/:id', deleteUser);

module.exports = router;
