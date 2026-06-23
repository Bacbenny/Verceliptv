import { Router, type IRouter } from "express";
import healthRouter from "./health";
import playlistRouter from "./playlist";
import tieulamRelayRouter from "./tieulam-relay";
import tokenRouter from "./token";

const router: IRouter = Router();

router.use(healthRouter);
router.use(playlistRouter);
router.use(tieulamRelayRouter);
router.use(tokenRouter);

export default router;
