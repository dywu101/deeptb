import torch
import torch as th
import numpy as np
import logging
import re
from dptb.hamiltonian.transform_sk import RotationSK
from dptb.nnsktb.formula import SKFormula
from dptb.utils.constants import anglrMId
from dptb.hamiltonian.soc import creat_basis_lm, get_matrix_lmbasis

''' Over use of different index system cause the symbols and type and index kind of object need to be recalculated in different 
Class, this makes entanglement of classes difficult. Need to design an consistent index system to resolve.'''

log = logging.getLogger(__name__)

class HamilEig(RotationSK):
    """ This module is to build the Hamiltonian from the SK-type bond integral.
    """
    def __init__(self, dtype=torch.float32, device='cpu') -> None:
        super().__init__(rot_type=dtype, device=device)
        self.dtype = dtype
        if self.dtype is th.float32:
            self.cdtype = th.complex64
        elif self.dtype is th.float64:
            self.cdtype = th.complex128
        self.use_orthogonal_basis = False
        self.hamil_blocks = None
        self.overlap_blocks = None
        self.device = device

    def update_hs_list(self, struct, hoppings, onsiteEs, onsiteVs=None, overlaps=None, onsiteSs=None, soc_lambdas=None, **options):
        '''It updates the bond structure, bond type, bond type id, bond hopping, bond onsite, hopping, onsite
        energy, overlap, and onsite spin
        
        Parameters
        ----------
        hoppings
            a list bond integral for hoppings.
        onsiteEs
            a list of onsite energy for each atom and each orbital.
        overlaps
            a list bond integral for overlaps.
        onsiteSs
            a list of onsite overlaps for each atom and each orbital.
        '''
        self.__struct__ = struct
        self.hoppings = hoppings
        self.onsiteEs = onsiteEs
        self.onsiteVs = onsiteVs
        self.soc_lambdas = soc_lambdas
        self.use_orthogonal_basis = False
        if overlaps is None:
            self.use_orthogonal_basis = True
        else:
            self.overlaps = overlaps
            self.onsiteSs = onsiteSs
            self.use_orthogonal_basis = False
        
        if soc_lambdas is None:
            self.soc = False

        self.num_orbs_per_atom = []
        for itype in self.__struct__.proj_atom_symbols:
            norbs = self.__struct__.proj_atomtype_norbs[itype]
            self.num_orbs_per_atom.append(norbs)

    def get_soc_block(self, bonds_onsite = None):
        numOrbs = np.array(self.num_orbs_per_atom)
        totalOrbs = np.sum(numOrbs)
        if bonds_onsite is None:
            _, bonds_onsite = self.__struct__.get_bond()

        soc_diag = torch.zeros_like((totalOrbs, totalOrbs), device=self.device, dtype=self.cdtype)
        soc_up = torch.zeros_like((totalOrbs, totalOrbs), device=self.device, dtype=self.cdtype)

        # compute soc mat for each atom:
        soc_atom_diag = self.__struct__.get("soc_atom_diag", {})
        soc_atom_up = self.__struct__.get("soc_atom_up", {})
        if not soc_atom_diag or not soc_atom_up:
            for iatype in self.__struct__.proj_atomtype:
                tmp_diag = torch.zeros([self.__struct__.proj_atomtype_norbs[iatype], self.__struct__.proj_atomtype_norbs[iatype]], dtype=self.cdtype, device=self.device)
                tmp_up = torch.zeros([self.__struct__.proj_atomtype_norbs[iatype], self.__struct__.proj_atomtype_norbs[iatype]], dtype=self.cdtype, device=self.device)

                ist = 0
                for ish in self.__struct__.proj_atom_anglr_m[iatype]:
                    ishsymbol = ''.join(re.findall(r'[A-Za-z]',ish))
                    shidi = anglrMId[ishsymbol]          # 0,1,2,...
                    norbi = 2*shidi + 1

                    soc_orb = get_matrix_lmbasis(creat_basis_lm(ishsymbol, device=self.device, dtype=self.dtype))

                    tmp_diag[ist:ist+norbi, ist:ist+norbi] = soc_orb[:norbi,:norbi]
                    tmp_up[ist:ist+norbi, ist:ist+norbi] = soc_orb[:norbi, norbi:]
                    ist = ist + norbi

                soc_atom_diag.update({iatype:tmp_diag})
                soc_atom_up.update({iatype:tmp_up})
            self.__struct__.soc_atom_diag = soc_atom_diag
            self.__struct__.soc_atom_up = soc_atom_up
        
        for ib in range(len(bonds_onsite)):
            ibond = bonds_onsite[ib].astype(int)
            iatom = ibond[1]
            ist = int(np.sum(numOrbs[0:iatom]))
            ied = int(np.sum(numOrbs[0:iatom+1]))
            iatype = self.__struct__.proj_atom_symbols[iatom]

            # get lambdas
            ist = 0
            lambdas = torch.zeros((ied-ist,), device=self.device, dtype=self.dtype)
            for ish in self.__struct__.proj_atom_anglr_m[iatype]:
                indx = self.__struct__.onsite_index_map[iatype][ish]
                shidi = anglrMId[ishsymbol]          # 0,1,2,...
                norbi = 2*shidi + 1
                lambdas[ist:ist+norbi] = self.soc_lambdas[ib][indx]
                ist = ist + norbi

            soc_diag[ist:ied,ist:ied] += soc_atom_diag[iatype] * torch.diag(lambdas)
            soc_up[ist:ied, ist:ied] += soc_atom_up[iatype] * torch.diag(lambdas)

        return soc_diag, soc_up
    
    def get_hs_onsite(self, bonds_onsite = None, onsite_envs=None):
        if bonds_onsite is None:
            _, bonds_onsite = self.__struct__.get_bond()
        onsiteH_blocks = []
        if not self.use_orthogonal_basis:
            onsiteS_blocks = []
        else:
            onsiteS_blocks = None
        
        iatom_to_onsite_index = {}
        for ib in range(len(bonds_onsite)):
            ibond = bonds_onsite[ib].astype(int)
            iatom = ibond[1]
            iatom_to_onsite_index.update({iatom:ib})
            jatom = ibond[3]
            iatype = self.__struct__.proj_atom_symbols[iatom]
            jatype = self.__struct__.proj_atom_symbols[jatom]
            assert iatype == jatype, "i type should equal j type."

            sub_hamil_block = th.zeros([self.__struct__.proj_atomtype_norbs[iatype], self.__struct__.proj_atomtype_norbs[jatype]], dtype=self.dtype, device=self.device)
            if not self.use_orthogonal_basis:
                sub_over_block = th.zeros([self.__struct__.proj_atomtype_norbs[iatype], self.__struct__.proj_atomtype_norbs[jatype]], dtype=self.dtype, device=self.device)
            
            ist = 0
            for ish in self.__struct__.proj_atom_anglr_m[iatype]:     # ['s','p',..]
                ishsymbol = ''.join(re.findall(r'[A-Za-z]',ish))
                shidi = anglrMId[ishsymbol]          # 0,1,2,...
                norbi = 2*shidi + 1 

                indx = self.__struct__.onsite_index_map[iatype][ish] # change onsite index map from {N:{s:}} to {N:{ss:, sp:}}
                sub_hamil_block[ist:ist+norbi, ist:ist+norbi] = th.eye(norbi, dtype=self.dtype, device=self.device) * self.onsiteEs[ib][indx]
                if not self.use_orthogonal_basis:
                    sub_over_block[ist:ist+norbi, ist:ist+norbi] = th.eye(norbi, dtype=self.dtype, device=self.device) * self.onsiteSs[ib][indx]
                ist = ist + norbi

            onsiteH_blocks.append(sub_hamil_block)
            if not self.use_orthogonal_basis:
                onsiteS_blocks.append(sub_over_block)

        # onsite strain
        if onsite_envs is not None:
            assert self.onsiteVs is not None
            for ib, env in enumerate(onsite_envs):
                
                iatype, iatom, jatype, jatom = self.__struct__.proj_atom_symbols[int(env[1])], env[1], self.__struct__.atom_symbols[int(env[3])], env[3]
                direction_vec = env[8:11].astype(np.float32)

                sub_hamil_block = th.zeros([self.__struct__.proj_atomtype_norbs[iatype], self.__struct__.proj_atomtype_norbs[iatype]], dtype=self.dtype, device=self.device)
            
                envtype = iatype + '-' + jatype

                ist = 0
                for ish in self.__struct__.proj_atom_anglr_m[iatype]:
                    ishsymbol = ''.join(re.findall(r'[A-Za-z]',ish))
                    shidi = anglrMId[ishsymbol]
                    norbi = 2*shidi+1
                    
                    jst = 0
                    for jsh in self.__struct__.proj_atom_anglr_m[iatype]:
                        jshsymbol = ''.join(re.findall(r'[A-Za-z]',jsh))
                        shidj = anglrMId[jshsymbol]
                        norbj = 2 * shidj + 1

                        idx = self.__struct__.onsite_strain_index_map[envtype][ish+'-'+jsh]
                        
                        if shidi < shidj:
                            
                            tmpH = self.rot_HS(Htype=ishsymbol+jshsymbol, Hvalue=self.onsiteVs[ib][idx], Angvec=direction_vec)
                            # Hamilblock[ist:ist+norbi, jst:jst+norbj] = th.transpose(tmpH,dim0=0,dim1=1)
                            sub_hamil_block[ist:ist+norbi, jst:jst+norbj] = th.transpose(tmpH,dim0=0,dim1=1)
                        else:
                            tmpH = self.rot_HS(Htype=jshsymbol+ishsymbol, Hvalue=self.onsiteVs[ib][idx], Angvec=direction_vec)
                            sub_hamil_block[ist:ist+norbi, jst:jst+norbj] = tmpH
                
                        jst = jst + norbj 
                    ist = ist + norbi
                onsiteH_blocks[iatom_to_onsite_index[iatom]] += sub_hamil_block

        return onsiteH_blocks, onsiteS_blocks, bonds_onsite
    
    def get_hs_hopping(self, bonds_hoppings = None):
        if bonds_hoppings is None:
            bonds_hoppings, _ = self.__struct__.get_bond()

        hoppingH_blocks = []
        if not self.use_orthogonal_basis:
            hoppingS_blocks = []
        else:
            hoppingS_blocks = None
        
        for ib in range(len(bonds_hoppings)):
            
            ibond = bonds_hoppings[ib,0:7].astype(int)
            #direction_vec = (self.__struct__.projected_struct.positions[ibond[3]]
            #          - self.__struct__.projected_struct.positions[ibond[1]]
            #          + np.dot(ibond[4:], self.__struct__.projected_struct.cell))
            #dist = np.linalg.norm(direction_vec)
            #direction_vec = direction_vec/dist
            direction_vec = bonds_hoppings[ib,8:11].astype(np.float32)
            iatype = self.__struct__.proj_atom_symbols[ibond[1]]
            jatype = self.__struct__.proj_atom_symbols[ibond[3]]

            sub_hamil_block = th.zeros([self.__struct__.proj_atomtype_norbs[iatype], self.__struct__.proj_atomtype_norbs[jatype]], dtype=self.dtype, device=self.device)
            if not self.use_orthogonal_basis:
                sub_over_block = th.zeros([self.__struct__.proj_atomtype_norbs[iatype], self.__struct__.proj_atomtype_norbs[jatype]], dtype=self.dtype, device=self.device)
            
            bondatomtype = iatype + '-' + jatype
            
            ist = 0
            for ish in self.__struct__.proj_atom_anglr_m[iatype]:
                ishsymbol = ''.join(re.findall(r'[A-Za-z]',ish))
                shidi = anglrMId[ishsymbol]
                norbi = 2*shidi+1
                
                jst = 0
                for jsh in self.__struct__.proj_atom_anglr_m[jatype]:
                    jshsymbol = ''.join(re.findall(r'[A-Za-z]',jsh))
                    shidj = anglrMId[jshsymbol]
                    norbj = 2 * shidj + 1

                    idx = self.__struct__.bond_index_map[bondatomtype][ish+'-'+jsh]
                    if shidi < shidj:
                        tmpH = self.rot_HS(Htype=ishsymbol+jshsymbol, Hvalue=self.hoppings[ib][idx], Angvec=direction_vec)
                        # Hamilblock[ist:ist+norbi, jst:jst+norbj] = th.transpose(tmpH,dim0=0,dim1=1)
                        sub_hamil_block[ist:ist+norbi, jst:jst+norbj] = (-1.0)**(shidi + shidj) * th.transpose(tmpH,dim0=0,dim1=1)
                        if not self.use_orthogonal_basis:
                            tmpS = self.rot_HS(Htype=ishsymbol+jshsymbol, Hvalue=self.overlaps[ib][idx], Angvec=direction_vec)
                        # Soverblock[ist:ist+norbi, jst:jst+norbj] = th.transpose(tmpS,dim0=0,dim1=1)
                            sub_over_block[ist:ist+norbi, jst:jst+norbj] = (-1.0)**(shidi + shidj) * th.transpose(tmpS,dim0=0,dim1=1)
                    else:
                        tmpH = self.rot_HS(Htype=jshsymbol+ishsymbol, Hvalue=self.hoppings[ib][idx], Angvec=direction_vec)
                        sub_hamil_block[ist:ist+norbi, jst:jst+norbj] = tmpH
                        if not self.use_orthogonal_basis:
                            tmpS = self.rot_HS(Htype=jshsymbol+ishsymbol, Hvalue = self.overlaps[ib][idx], Angvec = direction_vec)
                            sub_over_block[ist:ist+norbi, jst:jst+norbj] = tmpS
                
                    jst = jst + norbj 
                ist = ist + norbi   
            
            hoppingH_blocks.append(sub_hamil_block)
            if not self.use_orthogonal_basis:
                hoppingS_blocks.append(sub_over_block)

        return hoppingH_blocks, hoppingS_blocks, bonds_hoppings
    
    def get_hs_blocks(self, bonds_onsite = None, bonds_hoppings=None, onsite_envs=None):
        onsiteH, onsiteS, bonds_onsite = self.get_hs_onsite(bonds_onsite=bonds_onsite, onsite_envs=onsite_envs)
        hoppingH, hoppingS, bonds_hoppings = self.get_hs_hopping(bonds_hoppings=bonds_hoppings)

        self.all_bonds = np.concatenate([bonds_onsite[:,0:7],bonds_hoppings[:,0:7]],axis=0)
        self.all_bonds = self.all_bonds.astype(int)
        onsiteH.extend(hoppingH)
        self.hamil_blocks = onsiteH
        if not self.use_orthogonal_basis:
            onsiteS.extend(hoppingS)
            self.overlap_blocks = onsiteS
        if self.soc:
            self.soc_diag, self.soc_up = self.get_soc_block(bonds_onsite=bonds_onsite)

        return True

    def hs_block_R2k(self, kpoints, HorS='H', time_symm=True):
        '''The function takes in a list of Hamiltonian matrices for each bond, and a list of k-points, and
        returns a list of Hamiltonian matrices for each k-point

        Parameters
        ----------
        HorS
            string, 'H' or 'S' to indicate for Hk or Sk calculation.
        kpoints
            the k-points in the path.
        time_symm, optional
            if True, the Hamiltonian is time-reversal symmetric, defaults to True (optional)
        dtype, optional
            'tensor' or 'numpy', defaults to tensor (optional)

        Returns
        -------
            A list of Hamiltonian or Overlap matrices for each k-point.
        ''' 

        numOrbs = np.array(self.num_orbs_per_atom)
        totalOrbs = np.sum(numOrbs)
        if HorS == 'H':
            hijAll = self.hamil_blocks
        elif HorS == 'S':
            hijAll = self.overlap_blocks
        else:
            print("HorS should be 'H' or 'S' !")

        if self.soc:
            Hk = th.zeros([len(kpoints), 2*totalOrbs, 2*totalOrbs], dtype = self.cdtype, device=self.device)
        else:
            Hk = th.zeros([len(kpoints), totalOrbs, totalOrbs], dtype = self.cdtype, device=self.device)

        for ik in range(len(kpoints)):
            k = kpoints[ik]
            hk = th.zeros([totalOrbs,totalOrbs],dtype = self.cdtype, device=self.device)
            for ib in range(len(self.all_bonds)):
                Rlatt = self.all_bonds[ib,4:7].astype(int)
                i = self.all_bonds[ib,1].astype(int)
                j = self.all_bonds[ib,3].astype(int)
                ist = int(np.sum(numOrbs[0:i]))
                ied = int(np.sum(numOrbs[0:i+1]))
                jst = int(np.sum(numOrbs[0:j]))
                jed = int(np.sum(numOrbs[0:j+1]))
                if ib < len(numOrbs): 
                    """
                    len(numOrbs)= numatoms. the first numatoms are onsite energies.
                    if turn on timeSymm when generating the bond list <i,j>. only i>= or <= j are included. 
                    if turn off timeSymm when generating the bond list <i,j>. all the i j are included.
                    for case 1, H = H+H^\dagger to get the full matrix, the the onsite one is doubled.
                    for case 2. no need to do H = H+H^dagger. since the matrix is already full.
                    """
                    if time_symm:
                        hk[ist:ied,jst:jed] += 0.5 * hijAll[ib] * np.exp(-1j * 2 * np.pi* np.dot(k,Rlatt))
                    else:
                        hk[ist:ied,jst:jed] += hijAll[ib] * np.exp(-1j * 2 * np.pi* np.dot(k,Rlatt)) 
                else:
                    hk[ist:ied,jst:jed] += hijAll[ib] * np.exp(-1j * 2 * np.pi* np.dot(k,Rlatt)) 
            if time_symm:
                hk = hk + hk.T.conj()
            if self.soc:
                hk = torch.kron(A=torch.eye(2, device=self.device, dtype=self.dtype), B=hk)
            Hk[ik] = hk
        
        if self.soc:
            Hk[:, :totalOrbs, :totalOrbs] += self.soc_diag.unsqueeze(0)
            Hk[:, totalOrbs:, totalOrbs:] += self.soc_diag.conj().unsqueeze(0)
            Hk[:, :totalOrbs, totalOrbs:] += self.soc_up.unsqueeze(0)
            Hk[:, totalOrbs:, :totalOrbs] += self.soc_up.conj().unsqueeze(0)
            
        return Hk

    def Eigenvalues(self, kpoints, time_symm=True):
        """ using the tight-binding H and S matrix calculate eigenvalues at kpoints.
        
        Args:
            kpoints: the k-kpoints used to calculate the eigenvalues.
        Note: must have the BondHBlock and BondSBlock 
        """
        hkmat = self.hs_block_R2k(kpoints=kpoints, HorS='H', time_symm=time_symm)
        if not self.use_orthogonal_basis:
            skmat =  self.hs_block_R2k(kpoints=kpoints, HorS='S', time_symm=time_symm)
        else:
            skmat = torch.eye(hkmat.shape[1], dtype=self.cdtype).unsqueeze(0).repeat(hkmat.shape[0], 1, 1)

        chklowt = th.linalg.cholesky(skmat)
        chklowtinv = th.linalg.inv(chklowt)
        Heff = (chklowtinv @ hkmat @ th.transpose(chklowtinv,dim0=1,dim1=2).conj())
        # the factor 13.605662285137 * 2 from Hartree to eV.
        # eigks = th.linalg.eigvalsh(Heff) * 13.605662285137 * 2
        eigks, Q = th.linalg.eigh(Heff)
        eigks = eigks * 13.605662285137 * 2
        Qres = Q.detach()
        # else:
        #     chklowt = np.linalg.cholesky(skmat)
        #     chklowtinv = np.linalg.inv(chklowt)
        #     Heff = (chklowtinv @ hkmat @ np.transpose(chklowtinv,(0,2,1)).conj())
        #     eigks = np.linalg.eigvalsh(Heff) * 13.605662285137 * 2
        #     Qres = 0

        return eigks, Qres