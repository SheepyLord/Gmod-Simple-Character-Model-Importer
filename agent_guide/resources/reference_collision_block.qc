$collisionjoints "Physics.smd" 
{
	$mass 48 
	$inertia 12 
	$damping 0.8 
	$rotdamping 4 
	$rootbone "ValveBiped.Bip01_Pelvis" 

	$jointrotdamping "ValveBiped.Bip01_Pelvis" 3 

	$jointmassbias "ValveBiped.Bip01_Spine1" 8 
	$jointrotdamping "ValveBiped.Bip01_Spine1" 5 
	$jointconstrain "ValveBiped.Bip01_Spine1" x limit -10 10 0 
	$jointconstrain "ValveBiped.Bip01_Spine1" y limit -16 16 0 
	$jointconstrain "ValveBiped.Bip01_Spine1" z limit -19 19 0 

	$jointmassbias "ValveBiped.Bip01_Spine4" 9 
	$jointrotdamping "ValveBiped.Bip01_Spine4" 5 
	$jointconstrain "ValveBiped.Bip01_Spine4" x limit -10 10 0 
	$jointconstrain "ValveBiped.Bip01_Spine4" y limit -10 10 0 
	$jointconstrain "ValveBiped.Bip01_Spine4" z limit -20 20 0 

	$jointmassbias "ValveBiped.Bip01_R_Clavicle" 4 
	$jointrotdamping "ValveBiped.Bip01_R_Clavicle" 6 
	$jointconstrain "ValveBiped.Bip01_R_Clavicle" x limit -10 10 0 
	$jointconstrain "ValveBiped.Bip01_R_Clavicle" y limit -5 5 0 
	$jointconstrain "ValveBiped.Bip01_R_Clavicle" z limit 0 15 0 

	$jointmassbias "ValveBiped.Bip01_R_UpperArm" 5 
	$jointconstrain "ValveBiped.Bip01_R_UpperArm" x limit -15 20 0 
	$jointconstrain "ValveBiped.Bip01_R_UpperArm" y limit -40 32 0 
	$jointconstrain "ValveBiped.Bip01_R_UpperArm" z limit -80 25 0 

	$jointmassbias "ValveBiped.Bip01_L_Clavicle" 4 
	$jointrotdamping "ValveBiped.Bip01_L_Clavicle" 6 
	$jointconstrain "ValveBiped.Bip01_L_Clavicle" x limit -10 10 0 
	$jointconstrain "ValveBiped.Bip01_L_Clavicle" y limit -5 5 0 
	$jointconstrain "ValveBiped.Bip01_L_Clavicle" z limit 0 15 0 

	$jointmassbias "ValveBiped.Bip01_L_UpperArm" 5 
	$jointconstrain "ValveBiped.Bip01_L_UpperArm" x limit -15 20 0 
	$jointconstrain "ValveBiped.Bip01_L_UpperArm" y limit -40 32 0 
	$jointconstrain "ValveBiped.Bip01_L_UpperArm" z limit -80 25 0 

	$jointmassbias "ValveBiped.Bip01_L_Forearm" 4 
	$jointrotdamping "ValveBiped.Bip01_L_Forearm" 4 
	$jointconstrain "ValveBiped.Bip01_L_Forearm" x limit -40 15 0 
	$jointconstrain "ValveBiped.Bip01_L_Forearm" y limit 0 0 0 
	$jointconstrain "ValveBiped.Bip01_L_Forearm" z limit -120 10 0 

	$jointrotdamping "ValveBiped.Bip01_L_Hand" 1 
	$jointconstrain "ValveBiped.Bip01_L_Hand" x limit -25 25 0 
	$jointconstrain "ValveBiped.Bip01_L_Hand" y limit -35 35 0 
	$jointconstrain "ValveBiped.Bip01_L_Hand" z limit -50 50 0 

	$jointmassbias "ValveBiped.Bip01_R_Forearm" 4 
	$jointrotdamping "ValveBiped.Bip01_R_Forearm" 4 
	$jointconstrain "ValveBiped.Bip01_R_Forearm" x limit -40 15 0 
	$jointconstrain "ValveBiped.Bip01_R_Forearm" y limit 0 0 0 
	$jointconstrain "ValveBiped.Bip01_R_Forearm" z limit -120 10 0 

	$jointrotdamping "ValveBiped.Bip01_R_Hand" 1 
	$jointconstrain "ValveBiped.Bip01_R_Hand" x limit -25 25 0 
	$jointconstrain "ValveBiped.Bip01_R_Hand" y limit -35 35 0 
	$jointconstrain "ValveBiped.Bip01_R_Hand" z limit -50 50 0 

	$jointmassbias "ValveBiped.Bip01_Head1" 4 
	$jointrotdamping "ValveBiped.Bip01_Head1" 3 
	$jointconstrain "ValveBiped.Bip01_Head1" x limit -50 50 0 
	$jointconstrain "ValveBiped.Bip01_Head1" y limit -20 20 0 
	$jointconstrain "ValveBiped.Bip01_Head1" z limit -26 30 0 

	$jointmassbias "ValveBiped.Bip01_R_Thigh" 7 
	$jointrotdamping "ValveBiped.Bip01_R_Thigh" 7 
	$jointconstrain "ValveBiped.Bip01_R_Thigh" x limit -30 30 0 
	$jointconstrain "ValveBiped.Bip01_R_Thigh" y limit -60 30 0 
	$jointconstrain "ValveBiped.Bip01_R_Thigh" z limit -100 30 0 

	$jointmassbias "ValveBiped.Bip01_R_Calf" 4 
	$jointrotdamping "ValveBiped.Bip01_R_Calf" 5 
	$jointconstrain "ValveBiped.Bip01_R_Calf" x limit -15 15 0 
	$jointconstrain "ValveBiped.Bip01_R_Calf" y limit -5 5 0 
	$jointconstrain "ValveBiped.Bip01_R_Calf" z limit -10 125 0 

	$jointrotdamping "ValveBiped.Bip01_R_Foot" 9 
	$jointconstrain "ValveBiped.Bip01_R_Foot" x limit -15 15 0 
	$jointconstrain "ValveBiped.Bip01_R_Foot" y limit -15 15 0 
	$jointconstrain "ValveBiped.Bip01_R_Foot" z limit -18 25 0 

	$jointmassbias "ValveBiped.Bip01_L_Thigh" 7 
	$jointrotdamping "ValveBiped.Bip01_L_Thigh" 7 
	$jointconstrain "ValveBiped.Bip01_L_Thigh" x limit -30 30 0 
	$jointconstrain "ValveBiped.Bip01_L_Thigh" y limit -30 60 0 
	$jointconstrain "ValveBiped.Bip01_L_Thigh" z limit -100 30 0 

	$jointmassbias "ValveBiped.Bip01_L_Calf" 4 
	$jointrotdamping "ValveBiped.Bip01_L_Calf" 5 
	$jointconstrain "ValveBiped.Bip01_L_Calf" x limit -15 15 0 
	$jointconstrain "ValveBiped.Bip01_L_Calf" y limit -5 5 0 
	$jointconstrain "ValveBiped.Bip01_L_Calf" z limit -10 125 0 

	$jointrotdamping "ValveBiped.Bip01_L_Foot" 9 
	$jointconstrain "ValveBiped.Bip01_L_Foot" x limit -15 15 0 
	$jointconstrain "ValveBiped.Bip01_L_Foot" y limit -15 15 0 
	$jointconstrain "ValveBiped.Bip01_L_Foot" z limit -18 25 0 

	$jointconstrain "Sweet_0_0" x limit -80 2 0 
	$jointconstrain "Sweet_0_0" y limit -2 2 0 
	$jointconstrain "Sweet_0_0" z limit -2 2 0 

	$jointconstrain "Sweet_0_3" x limit -2 2 0 
	$jointconstrain "Sweet_0_3" y limit -2 80 0 
	$jointconstrain "Sweet_0_3" z limit -2 2 0 

	$jointconstrain "Sweet_0_6" x limit -2 80 0 
	$jointconstrain "Sweet_0_6" y limit -2 2 0 
	$jointconstrain "Sweet_0_6" z limit -2 2 0 

	$jointconstrain "Sweet_0_10" x limit -2 80 0 
	$jointconstrain "Sweet_0_10" y limit -2 2 0 
	$jointconstrain "Sweet_0_10" z limit -2 2 0 

	$jointconstrain "Sweet_0_13" x limit -2 2 0 
	$jointconstrain "Sweet_0_13" y limit -80 2 0 
	$jointconstrain "Sweet_0_13" z limit -2 2 0 

	$jointconstrain "Sleeve_0_1_R" x limit -100 100 0 
	$jointconstrain "Sleeve_0_1_R" y limit -45 45 0 
	$jointconstrain "Sleeve_0_1_R" z limit -30 30 0 

	$jointconstrain "Sleeve_0_1_L" x limit -100 100 0 
	$jointconstrain "Sleeve_0_1_L" y limit -45 45 0 
	$jointconstrain "Sleeve_0_1_L" z limit -30 30 0 

	$jointconstrain "TailF1" x limit -30 105 0 
	$jointconstrain "TailF1" y limit -30 30 0 
	$jointconstrain "TailF1" z limit -15 15 0 

	$jointconstrain "TailF3" x limit -30 105 0 
	$jointconstrain "TailF3" y limit -30 30 0 
	$jointconstrain "TailF3" z limit -15 15 0 

	$jointconstrain "TailF5" x limit -30 105 0 
	$jointconstrain "TailF5" y limit -30 30 0 
	$jointconstrain "TailF5" z limit -15 15 0 

	$jointconstrain "Hair_F1_L" x limit -30 105 0 
	$jointconstrain "Hair_F1_L" y limit -30 30 0 
	$jointconstrain "Hair_F1_L" z limit -15 15 0 

	$jointconstrain "Hair_F5_L" x limit -30 105 0 
	$jointconstrain "Hair_F5_L" y limit -30 30 0 
	$jointconstrain "Hair_F5_L" z limit -15 15 0 

	$jointconstrain "Hair_F1_R" x limit -30 105 0 
	$jointconstrain "Hair_F1_R" y limit -30 30 0 
	$jointconstrain "Hair_F1_R" z limit -15 15 0 

	$jointconstrain "Hair_F5_R" x limit -30 105 0 
	$jointconstrain "Hair_F5_R" y limit -30 30 0 
	$jointconstrain "Hair_F5_R" z limit -15 15 0 

}

$collisiontext
{
	animatedfriction
	{
		animfrictionmin		"80.000000"
		animfrictionmax		"600.000000"
		animfrictiontimein		"0.150000"
		animfrictiontimeout		"0.250000"
		animfrictiontimehold		"1.750000"
	}
	editparams
	{
		rootname		"valvebiped.bip01_pelvis"
		totalmass		"50.000000"
	}
}
